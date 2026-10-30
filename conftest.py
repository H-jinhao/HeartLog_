"""Shared pytest fixtures.

Every test gets its own temporary SQLite database.  The real
``data/heartlog.db`` is never opened by the test suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        auto_create_schema=True,
        api_token=None,
        admin_token="test-admin-token",
        max_diary_chars=50_000,
        ai_enabled=False,
        ai_consent=False,
        ai_provider="disabled",
        _env_file=None,
    )


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_session(app) -> Iterator[Session]:
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def create_diary(client: TestClient, content: str = "她今天没有回复我的消息，我有点失落。") -> dict:
    response = client.post("/api/v1/diaries", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def diary_factory(client: TestClient):
    def _factory(content: str = "她今天没有回复我的消息，我有点失落。") -> dict:
        return create_diary(client, content)

    return _factory
