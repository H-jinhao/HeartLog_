"""Tests for deployment hardening: in-process rate limiting and warnings.

The limiter is disabled by default, so these tests construct their own
``Settings``/``create_app`` pairs instead of relying on the shared ``client``
fixture.  Nothing here touches the real ``data/heartlog.db``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.rate_limit import (
    SlidingWindowRateLimiter,
    build_rate_limiter,
    client_key_from_request,
    is_rate_limited_path,
)
from app.main import create_app

API = "/api/v1"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "environment": "test",
        "database_url": f"sqlite:///{(tmp_path / 'rate_limit.db').as_posix()}",
        "data_dir": tmp_path / "data",
        "backup_dir": tmp_path / "backups",
        "auto_create_schema": True,
        "api_token": None,
        "admin_token": "test-admin-token",
        "ai_enabled": False,
        "ai_consent": False,
        "ai_provider": "disabled",
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@contextmanager
def _client(tmp_path: Path, **overrides: object) -> Iterator[tuple[TestClient, object]]:
    settings = _settings(tmp_path, **overrides)
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, app
    finally:
        app.state.engine.dispose()


@pytest.fixture(autouse=True)
def _keep_loggers_enabled() -> Iterator[None]:
    """Alembic fileConfig in another test module disables existing loggers.

    Keep this module's log assertions independent of test execution order.
    """

    names = ("heartlog", "heartlog.rate_limit")
    state = [(logging.getLogger(name).disabled, logging.getLogger(name).level) for name in names]
    for name in names:
        logger = logging.getLogger(name)
        logger.disabled = False
        logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        for name, (disabled, level) in zip(names, state, strict=True):
            logger = logging.getLogger(name)
            logger.disabled = disabled
            logger.setLevel(level)


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    @property
    def blob(self) -> str:
        return "\n".join(self.messages)


# ---------------------------------------------------------------------------
# Configuration defaults / parsing
# ---------------------------------------------------------------------------
def test_rate_limit_defaults_are_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.rate_limit_enabled is False
    assert settings.trust_proxy_headers is False
    assert settings.rate_limit_requests == 240
    assert settings.rate_limit_window_seconds == 60
    assert settings.rate_limit_exempt_path_set >= {"/health", "/docs", "/openapi.json"}


def test_rate_limit_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEARTLOG_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HEARTLOG_RATE_LIMIT_REQUESTS", "7")
    monkeypatch.setenv("HEARTLOG_RATE_LIMIT_WINDOW_SECONDS", "30")
    monkeypatch.setenv("HEARTLOG_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("HEARTLOG_RATE_LIMIT_EXEMPT_PATHS", "/health, /docs/ ,,")
    settings = Settings(_env_file=None)
    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_requests == 7
    assert settings.rate_limit_window_seconds == 30
    assert settings.trust_proxy_headers is True
    assert settings.rate_limit_exempt_paths == "/health,/docs"
    assert settings.rate_limit_exempt_path_set == frozenset({"/health", "/docs"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_limit_requests", 0),
        ("rate_limit_window_seconds", 0),
        ("rate_limit_exempt_paths", "health"),
    ],
)
def test_invalid_rate_limit_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


def test_is_rate_limited_path_scope() -> None:
    exempt = frozenset({"/health", "/docs", "/openapi.json"})
    assert is_rate_limited_path("/api/v1/diaries", api_prefix="/api/v1", exempt_paths=exempt) is True
    assert is_rate_limited_path("/api/v1", api_prefix="/api/v1", exempt_paths=exempt) is True
    assert is_rate_limited_path("/api/v10/diaries", api_prefix="/api/v1", exempt_paths=exempt) is False
    assert is_rate_limited_path("/health", api_prefix="/api/v1", exempt_paths=exempt) is False
    assert is_rate_limited_path("/docs", api_prefix="/api/v1", exempt_paths=exempt) is False
    assert is_rate_limited_path("/docs/oauth2-redirect", api_prefix="/api/v1", exempt_paths=exempt) is False
    assert is_rate_limited_path("/openapi.json", api_prefix="/api/v1", exempt_paths=exempt) is False
    assert is_rate_limited_path("/other", api_prefix="/api/v1", exempt_paths=exempt) is False


def test_build_rate_limiter_from_settings(tmp_path: Path) -> None:
    limiter = build_rate_limiter(_settings(tmp_path, rate_limit_requests=9, rate_limit_window_seconds=25))
    assert limiter.limit == 9
    assert limiter.window_seconds == 25


# ---------------------------------------------------------------------------
# Middleware integration
# ---------------------------------------------------------------------------
def test_disabled_by_default_has_no_effect(tmp_path: Path) -> None:
    # Even a numerically tiny limit must be ignored while the feature is off.
    with _client(tmp_path, rate_limit_requests=1) as (client, _app):
        for _ in range(5):
            assert client.get(f"{API}/diaries").status_code == 200


def test_enabled_limit_allows_then_returns_429_with_headers(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
    ) as (client, _app):
        for _ in range(3):
            assert client.get(f"{API}/diaries").status_code == 200
        response = client.get(f"{API}/diaries")
        assert response.status_code == 429
        payload = response.json()
        assert payload["error"]["code"] == "rate_limited"
        assert payload["error"]["details"] == {
            "limit": 3,
            "window_seconds": 60,
            "retry_after_seconds": int(response.headers["Retry-After"]),
        }
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert response.headers["X-RateLimit-Limit"] == "3"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert 1 <= int(response.headers["X-RateLimit-Reset"]) <= 60
        # The existing request-context middleware must wrap the rejection too.
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "no-store"


def test_health_and_docs_are_exempt_and_not_counted(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    ) as (client, _app):
        for path in ("/health", "/docs", "/openapi.json", "/redoc"):
            for _ in range(5):
                assert client.get(path).status_code == 200
        assert client.get(f"{API}/diaries").status_code == 200
        # The health/docs traffic above must not have consumed the API budget.
        assert client.get(f"{API}/diaries").status_code == 429


def test_rate_limit_protects_even_with_wrong_auth_header(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        api_token="deployment-token",
    ) as (client, _app):
        assert client.get(f"{API}/diaries", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 401
        assert client.get(f"{API}/diaries").status_code == 401
        limited = client.get(f"{API}/diaries")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"


def test_trust_proxy_false_ignores_spoofed_forwarded_for(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
        trust_proxy_headers=False,
    ) as (client, _app):
        spoofed = "209.200.100.42"
        assert client.get(f"{API}/diaries", headers={"X-Forwarded-For": spoofed}).status_code == 200
        assert client.get(f"{API}/diaries", headers={"X-Forwarded-For": spoofed}).status_code == 200
        # Same direct peer, different spoofed header -> still limited.
        response = client.get(f"{API}/diaries", headers={"X-Forwarded-For": "198.51.100.1"})
        assert response.status_code == 429


def test_trust_proxy_true_uses_first_forwarded_hop(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
        trust_proxy_headers=True,
    ) as (client, _app):
        first = {"X-Forwarded-For": "198.51.100.10, 10.0.0.1"}
        second = {"X-Forwarded-For": "198.51.100.11, 10.0.0.1"}
        assert client.get(f"{API}/diaries", headers=first).status_code == 200
        assert client.get(f"{API}/diaries", headers=first).status_code == 200
        assert client.get(f"{API}/diaries", headers=first).status_code == 429
        # A different first hop has its own independent bucket.
        assert client.get(f"{API}/diaries", headers=second).status_code == 200
        assert client.get(f"{API}/diaries", headers=second).status_code == 200
        # The same first hop keeps its exhausted bucket regardless of later hops.
        assert client.get(f"{API}/diaries", headers=first).status_code == 429


def test_trust_proxy_true_malformed_header_falls_back_to_peer(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        trust_proxy_headers=True,
    ) as (client, _app):
        malformed = {"X-Forwarded-For": "not-an-ip"}
        for _ in range(2):
            assert client.get(f"{API}/diaries", headers=malformed).status_code == 200
        assert client.get(f"{API}/diaries", headers=malformed).status_code == 429


def test_bucket_refills_after_window_via_middleware(tmp_path: Path) -> None:
    now = [1_000.0]
    with _client(
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_seconds=10,
    ) as (client, app):
        app.state.rate_limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])
        assert client.get(f"{API}/diaries").status_code == 200
        assert client.get(f"{API}/diaries").status_code == 200
        limited = client.get(f"{API}/diaries")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "10"
        now[0] += 10.0
        assert client.get(f"{API}/diaries").status_code == 200


def test_rate_limit_logs_are_private(tmp_path: Path) -> None:
    collector = _LogCollector()
    app_logger = logging.getLogger("heartlog")
    rate_logger = logging.getLogger("heartlog.rate_limit")
    app_logger.addHandler(collector)
    rate_logger.addHandler(collector)
    private_text = "私密日记正文：她说周末会来找我。"
    try:
        with _client(
            tmp_path,
            rate_limit_enabled=True,
            rate_limit_requests=1,
        ) as (client, _app):
            created = client.post(f"{API}/diaries", json={"content": private_text})
            assert created.status_code == 201
            limited = client.get(
                f"{API}/diaries?q=private-query-value",
                headers={"X-Request-ID": "trace-42", "Authorization": "Bearer super-secret-token"},
            )
            assert limited.status_code == 429
            assert limited.headers["X-Request-ID"] == "trace-42"
    finally:
        app_logger.removeHandler(collector)
        rate_logger.removeHandler(collector)

    blob = collector.blob
    assert "rate_limited" in blob
    assert "request_id=trace-42" in blob
    assert "status=429" in blob  # access log still records the rejection
    assert private_text not in blob
    assert "private-query-value" not in blob
    assert "super-secret-token" not in blob


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_deployment_warnings_are_logged_for_non_local_envs(
    tmp_path: Path, environment: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(
        tmp_path,
        environment=environment,
        debug=True,
        api_token=None,
        admin_token=None,
    )
    with caplog.at_level(logging.WARNING, logger="heartlog"):
        app = create_app(settings)
        try:
            pass
        finally:
            app.state.engine.dispose()
    text = caplog.text
    assert "deployment_security" in text
    assert "HEARTLOG_API_TOKEN is not set" in text
    assert "HEARTLOG_ADMIN_TOKEN is not set" in text
    assert "HEARTLOG_DEBUG=true" in text
    assert "HEARTLOG_RATE_LIMIT_ENABLED=false" in text
    # Never log configured secret values.
    assert "test-admin-token" not in text


def test_deployment_warnings_absent_when_hardened(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(
        tmp_path,
        environment="production",
        debug=False,
        api_token="api-secret-value",
        admin_token="admin-secret-value",
        rate_limit_enabled=True,
    )
    with caplog.at_level(logging.WARNING, logger="heartlog"):
        app = create_app(settings)
        try:
            pass
        finally:
            app.state.engine.dispose()
    assert "deployment_security" not in caplog.text
    assert "api-secret-value" not in caplog.text
    assert "admin-secret-value" not in caplog.text


def test_deployment_warnings_only_for_non_local_environments(tmp_path: Path) -> None:
    for environment in ("local", "test"):
        settings = _settings(tmp_path, environment=environment, debug=True, api_token=None, admin_token=None)
        assert settings.deployment_warnings() == []


def test_deployment_warnings_content(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        environment="production",
        debug=True,
        api_token=None,
        admin_token=None,
        rate_limit_enabled=False,
        trust_proxy_headers=True,
    )
    warnings = settings.deployment_warnings()
    joined = " ".join(warnings)
    assert any("HEARTLOG_API_TOKEN" in item for item in warnings)
    assert any("HEARTLOG_ADMIN_TOKEN" in item for item in warnings)
    assert any("HEARTLOG_DEBUG" in item for item in warnings)
    assert any("HEARTLOG_RATE_LIMIT_ENABLED" in item for item in warnings)
    assert any("HEARTLOG_TRUST_PROXY_HEADERS" in item for item in warnings)
    assert len(warnings) == 5
    assert "test-admin-token" not in joined


# ---------------------------------------------------------------------------
# Limiter unit tests (no HTTP)
# ---------------------------------------------------------------------------
def test_sliding_window_retry_after_and_refill() -> None:
    now = [1_000.0]
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])
    first = limiter.check("client-a")
    assert first.allowed is True
    assert first.remaining == 1
    now[0] += 2.0
    second = limiter.check("client-a")
    assert second.allowed is True
    assert second.remaining == 0
    third = limiter.check("client-a")
    assert third.allowed is False
    assert third.retry_after == 8
    now[0] += 4.0
    assert limiter.check("client-a").retry_after == 4
    now[0] += 4.0  # the oldest accepted request (t=1000) has now left the window
    refilled = limiter.check("client-a")
    assert refilled.allowed is True
    assert refilled.remaining == 0


def test_clients_have_independent_buckets() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("client-a").allowed is True
    assert limiter.check("client-a").allowed is False
    assert limiter.check("client-b").allowed is True
    assert limiter.tracked_client_count == 2


def test_limiter_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=0, window_seconds=60)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=1, window_seconds=0)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=1, window_seconds=60, max_tracked_clients=0)


def test_limiter_map_is_bounded_and_stale_keys_are_purged() -> None:
    now = [500.0]
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window_seconds=30,
        max_tracked_clients=3,
        clock=lambda: now[0],
    )
    for index in range(20):
        limiter.check(f"client-{index}")
    assert limiter.tracked_client_count <= 3
    # After the window passes, every key is stale and the map is cleaned up.
    now[0] += 31.0
    limiter.check("fresh-client")
    assert limiter.tracked_client_count == 1


def test_limiter_evicts_least_recently_active_client_when_full() -> None:
    now = [1.0]
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window_seconds=60,
        max_tracked_clients=2,
        clock=lambda: now[0],
    )
    limiter.check("oldest")
    now[0] += 1.0
    limiter.check("newer")
    now[0] += 1.0
    limiter.check("newest")
    assert limiter.tracked_client_count == 2
    # "oldest" was evicted, so it starts with a fresh bucket...
    now[0] += 1.0
    result = limiter.check("oldest")
    assert result.allowed is True
    assert result.remaining == 4


def test_limiter_is_thread_safe_and_enforces_global_limit() -> None:
    limiter = SlidingWindowRateLimiter(limit=100, window_seconds=60)
    allowed_counts: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        count = 0
        try:
            for _ in range(200):
                if limiter.check(f"client-{index % 10}").allowed:
                    count += 1
        except BaseException as exc:  # pragma: no cover - defensive
            with lock:
                errors.append(exc)
        with lock:
            allowed_counts.append(count)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    # 10 client keys * limit 100; the window is far longer than the test.
    assert sum(allowed_counts) == 1_000


def test_client_key_helper_trusts_forwarded_header_only_when_configured() -> None:
    class _Peer:
        host = "127.0.0.1"

    class _Request:
        client = _Peer()
        headers = {"x-forwarded-for": "198.51.100.5, 10.0.0.1"}

    untrusted = client_key_from_request(_Request(), trust_proxy_headers=False)  # type: ignore[arg-type]
    trusted = client_key_from_request(_Request(), trust_proxy_headers=True)  # type: ignore[arg-type]
    assert untrusted == "127.0.0.1"
    assert trusted == "198.51.100.5"


def test_client_key_helper_handles_missing_peer() -> None:
    class _Request:
        client = None
        headers: dict[str, str] = {}

    assert client_key_from_request(_Request(), trust_proxy_headers=False) == "unknown"  # type: ignore[arg-type]


def test_reconfigure_resets_window_state() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("client").allowed is True
    assert limiter.check("client").allowed is False
    limiter.reconfigure(limit=2, window_seconds=120)
    assert limiter.limit == 2
    assert limiter.window_seconds == 120
    # State was cleared with the policy change.
    assert limiter.check("client").allowed is True
