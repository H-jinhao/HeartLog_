"""Productized backup tests: encryption, retention, scheduler, restore and drill.

All tests are offline and operate on ``tmp_path`` only.  They never open the
repository ``data/`` or ``backups/`` directories and never contact the network.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.models.memory import Memory, MemorySource
from app.services.backup_service import (
    DEFAULT_PASSPHRASE_ENV,
    MAGIC,
    BackupError,
    checksum_path,
    create_encrypted_backup,
    create_plaintext_backup,
    decrypt_file,
    drill_backup,
    is_encrypted,
    manifest_path,
    prune_backups,
    read_manifest,
    sha256_file,
    verify_backup_file,
)
from scripts import backup as backup_script
from scripts import backup_scheduler, restore, restore_drill
from tests.conftest import create_diary

PASSPHRASE = "correct horse battery staple 42"
PRIVATE_MARKER = "PRIVATE-DIARY-MARKER-9f3c"


@pytest.fixture(autouse=True)
def _clear_script_settings_cache():
    """Script-level ``get_settings()`` is cached; isolate env-based tests."""

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_source_db(path: Path, *, marker: str = PRIVATE_MARKER) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO sample (note) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()
    return path


def _seed_heartlog_db(client, app) -> dict:
    """Create a diary with two revisions plus a traceable memory."""

    diary = create_diary(client, f"第一条日记 {PRIVATE_MARKER}")
    updated = client.put(
        f"/api/v1/diaries/{diary['id']}",
        json={"content": f"修改后的日记 {PRIVATE_MARKER} v2", "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    revision = updated.json()["current_revision"]
    session = app.state.session_factory()
    try:
        memory = Memory(
            content="用户提到一个标记",
            memory_type="model_inference",
            source_type="diary_entry",
            source_entry_id=diary["id"],
            source_revision_id=revision["id"],
            verification_status="pending",
            created_by="model",
        )
        memory.sources.append(
            MemorySource(
                entry_id=diary["id"],
                revision_id=revision["id"],
                evidence_text="修改后的日记",
                start_offset=0,
                end_offset=6,
            )
        )
        session.add(memory)
        session.commit()
    finally:
        session.close()
    return {"diary": diary, "revision": revision}


def _configured_source_db(settings) -> Path:
    return Path(make_url(settings.database_url).database or "")


def _rewrite_metadata(backup: Path) -> None:
    """Recompute sidecar/manifest hashes (simulates an attacker hiding edits)."""

    digest = sha256_file(backup)
    checksum_path(backup).write_text(digest + "\n", encoding="utf-8")
    manifest = read_manifest(backup)
    assert manifest is not None
    manifest["sha256"] = digest
    manifest["size_bytes"] = backup.stat().st_size
    manifest_path(backup).write_text(json.dumps(manifest), encoding="utf-8")


def _set_backup_env(monkeypatch: pytest.MonkeyPatch, source: Path, backup_dir: Path) -> None:
    monkeypatch.setenv("HEARTLOG_DATABASE_URL", f"sqlite:///{source.as_posix()}")
    monkeypatch.setenv("HEARTLOG_BACKUP_DIR", str(backup_dir))
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Plaintext compatibility with the MVP scripts
# ---------------------------------------------------------------------------


def test_plaintext_backup_keeps_mvp_compatibility(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    destination = tmp_path / "backups" / "copy.db"

    manifest = backup_script.backup_database(source, destination)

    assert destination.exists()
    assert manifest["integrity_check"] == "ok"
    assert manifest["encrypted"] is False
    assert manifest["sha256"] == backup_script.sha256_file(destination)
    assert checksum_path(destination).exists()
    assert manifest_path(destination).exists()
    on_disk = json.loads(manifest_path(destination).read_text(encoding="utf-8"))
    assert on_disk["sha256"] == manifest["sha256"]
    with pytest.raises(SystemExit):
        backup_script.backup_database(source, destination)


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_encrypted_backup_roundtrip_does_not_leak_plaintext(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    destination = tmp_path / "backups" / "heartlog-20260101T000000Z.db.enc"
    staging_before = set(Path(tempfile.gettempdir()).glob("heartlog-encrypt-*"))

    manifest = create_encrypted_backup(source, destination, PASSPHRASE)

    assert set(Path(tempfile.gettempdir()).glob("heartlog-encrypt-*")) == staging_before
    assert is_encrypted(destination)
    assert destination.read_bytes().startswith(MAGIC)
    assert manifest["encrypted"] is True
    assert manifest["cipher"] == "AES-256-GCM"
    assert manifest["kdf"] == "scrypt"
    assert manifest["sha256"] == sha256_file(destination)
    assert len(manifest["kdf_params"]["salt_b64"]) > 0

    ciphertext = destination.read_bytes()
    assert PRIVATE_MARKER.encode() not in ciphertext
    assert PASSPHRASE.encode() not in ciphertext
    manifest_text = manifest_path(destination).read_text(encoding="utf-8")
    assert PASSPHRASE not in manifest_text
    assert PRIVATE_MARKER not in manifest_text

    decrypted = tmp_path / "decrypted.db"
    header = decrypt_file(destination, decrypted, PASSPHRASE)
    assert header["format"] == "heartlog.encrypted-backup"
    assert header["version"] == 1
    connection = sqlite3.connect(decrypted)
    try:
        assert connection.execute("SELECT note FROM sample").fetchone()[0] == PRIVATE_MARKER
    finally:
        connection.close()


def test_encrypted_backup_uses_fresh_salt_and_nonce(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    first = tmp_path / "first.db.enc"
    second = tmp_path / "second.db.enc"

    first_manifest = create_encrypted_backup(source, first, PASSPHRASE)
    second_manifest = create_encrypted_backup(source, second, PASSPHRASE)

    assert first_manifest["kdf_params"]["salt_b64"] != second_manifest["kdf_params"]["salt_b64"]
    assert first_manifest["nonce_b64"] != second_manifest["nonce_b64"]
    assert first.read_bytes() != second.read_bytes()


def test_wrong_passphrase_and_tampering_are_detected(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    encrypted = tmp_path / "backup.db.enc"
    create_encrypted_backup(source, encrypted, PASSPHRASE)

    wrong_target = tmp_path / "wrong.db"
    with pytest.raises(BackupError, match="wrong passphrase"):
        decrypt_file(encrypted, wrong_target, PASSPHRASE + "-wrong")
    assert not wrong_target.exists()

    tampered = bytearray(encrypted.read_bytes())
    tampered[-1] ^= 0xFF
    encrypted.write_bytes(bytes(tampered))
    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_backup_file(encrypted)

    with pytest.raises(BackupError, match="wrong passphrase|corrupted"):
        decrypt_file(encrypted, tmp_path / "tampered.db", PASSPHRASE)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_keeps_newest_and_never_deletes_unknown_files(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    for index in range(1, 6):
        create_plaintext_backup(source, backup_dir / f"heartlog-2026010{index}T000000Z.db")
    create_plaintext_backup(source, backup_dir / "manual-backup.db")
    orphan = backup_dir / "heartlog-20190101T000000Z.db"
    orphan.write_bytes(b"not a real backup")
    # Auto-named copy without its own metadata must never be deleted either.
    (backup_dir / "heartlog-20200101T000000Z.db").write_bytes(
        (backup_dir / "heartlog-20260105T000000Z.db").read_bytes()
    )

    deleted = prune_backups(backup_dir, 2)

    assert [path.name for path in deleted] == [
        "heartlog-20260101T000000Z.db",
        "heartlog-20260102T000000Z.db",
        "heartlog-20260103T000000Z.db",
    ]
    for index in (4, 5):
        kept = backup_dir / f"heartlog-2026010{index}T000000Z.db"
        assert kept.exists()
        assert checksum_path(kept).exists()
        assert manifest_path(kept).exists()
    for name in ("manual-backup.db", "manual-backup.db.sha256", "manual-backup.db.json"):
        assert (backup_dir / name).exists()
    assert orphan.read_bytes() == b"not a real backup"
    assert (backup_dir / "heartlog-20200101T000000Z.db").exists()

    assert prune_backups(backup_dir, 0) == []


def test_retention_skips_auto_named_backup_with_broken_metadata(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    first = backup_dir / "heartlog-20260101T000000Z.db"
    second = backup_dir / "heartlog-20260102T000000Z.db"
    create_plaintext_backup(source, first)
    create_plaintext_backup(source, second)
    manifest_path(first).write_text("{not valid json", encoding="utf-8")

    deleted = prune_backups(backup_dir, 1)

    assert deleted == []
    assert first.exists() and second.exists()


# ---------------------------------------------------------------------------
# CLI: backup encryption + retention wiring
# ---------------------------------------------------------------------------


def test_backup_cli_encrypt_and_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _set_backup_env(monkeypatch, source, backup_dir)
    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, PASSPHRASE)

    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        assert (
            backup_script.main(["--output", str(backup_dir / f"heartlog-{stamp}.db"), "--retention", "2"])
            == 0
        )
    plain_backups = sorted(backup_dir.glob("heartlog-*.db"))
    assert [path.name for path in plain_backups] == [
        "heartlog-20260102T000000Z.db",
        "heartlog-20260103T000000Z.db",
    ]

    assert backup_script.main(["--encrypt", "--retention", "0"]) == 0
    encrypted = list(backup_dir.glob("heartlog-*.db.enc"))
    assert len(encrypted) == 1
    assert is_encrypted(encrypted[0])
    manifest = read_manifest(encrypted[0])
    assert manifest is not None and manifest["encrypted"] is True

    monkeypatch.delenv(DEFAULT_PASSPHRASE_ENV)
    assert backup_script.main(["--encrypt", "--retention", "0"]) == 2


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def test_scheduler_run_once_encrypts_and_logs_only_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    _set_backup_env(monkeypatch, source, backup_dir)
    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, PASSPHRASE)

    with caplog.at_level(logging.INFO, logger="heartlog.backup_scheduler"):
        result = backup_scheduler.run_once(encrypt=True, retention=2, backup_dir=backup_dir)

    assert result["status"] == "ok"
    assert result["encrypted"] is True
    backups = list(backup_dir.glob("heartlog-*.db.enc"))
    assert len(backups) == 1
    assert is_encrypted(backups[0])
    assert PASSPHRASE not in caplog.text
    assert PASSPHRASE not in json.dumps(result)
    assert PRIVATE_MARKER not in json.dumps(result)


def test_scheduler_once_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _set_backup_env(monkeypatch, source, backup_dir)

    assert backup_scheduler.main(["--once", "--retention", "0"]) == 0
    assert len(list(backup_dir.glob("heartlog-*.db"))) == 1

    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, PASSPHRASE)
    assert backup_scheduler.main(["--once", "--encrypt", "--retention", "1"]) == 0
    assert len(list(backup_dir.glob("heartlog-*.db.enc"))) == 1

    monkeypatch.delenv(DEFAULT_PASSPHRASE_ENV)
    assert backup_scheduler.main(["--once", "--encrypt"]) == 2


def test_scheduler_loop_stops_via_stop_event_and_signal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls: list[dict] = []

    def fake_run_once(**kwargs) -> dict:
        calls.append(kwargs)
        stop.set()
        return {"status": "ok", "backup": "/tmp/fake.db", "encrypted": False, "deleted_count": 0}

    monkeypatch.setattr(backup_scheduler, "run_once", fake_run_once)
    assert backup_scheduler.run_loop(interval_seconds=3600, stop_event=stop) == 0
    assert len(calls) == 1

    signal_stop = threading.Event()
    handler = backup_scheduler.make_signal_handler(signal_stop)
    handler(15, None)
    assert signal_stop.is_set()


# ---------------------------------------------------------------------------
# Restore (encrypted + legacy plaintext) safety
# ---------------------------------------------------------------------------


def test_restore_encrypted_backup_creates_safety_backup(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db.enc"
    create_encrypted_backup(source, backup, PASSPHRASE)

    target = tmp_path / "live" / "heartlog.db"
    target.parent.mkdir()
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE previous (id INTEGER)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(BackupError, match="--force"):
        restore.restore_backup(backup, target, passphrase=PASSPHRASE)

    result = restore.restore_backup(backup, target, force=True, passphrase=PASSPHRASE)

    assert result["encrypted"] is True
    assert result["safety_backup"] is not None
    safety = Path(result["safety_backup"])
    assert safety.exists()
    assert manifest_path(safety).exists()
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT note FROM sample").fetchone()[0] == PRIVATE_MARKER
    finally:
        connection.close()


def test_restore_plaintext_backup_still_works(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db"
    create_plaintext_backup(source, backup)
    target = tmp_path / "live" / "heartlog.db"

    result = restore.restore_backup(backup, target)

    assert result["encrypted"] is False
    assert result["safety_backup"] is None
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT note FROM sample").fetchone()[0] == PRIVATE_MARKER
    finally:
        connection.close()


def test_restore_failure_never_replaces_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db.enc"
    create_encrypted_backup(source, backup, PASSPHRASE)
    target = tmp_path / "live" / "heartlog.db"
    target.parent.mkdir()
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE previous (id INTEGER)")
        connection.execute("INSERT INTO previous VALUES (7)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(BackupError, match="wrong passphrase"):
        restore.restore_backup(backup, target, force=True, passphrase="definitely-not-it")
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT id FROM previous").fetchone()[0] == 7
    finally:
        connection.close()

    monkeypatch.delenv(DEFAULT_PASSPHRASE_ENV, raising=False)
    with pytest.raises(BackupError, match=DEFAULT_PASSPHRASE_ENV):
        restore.restore_backup(backup, target, force=True)
    assert not (target.parent / (target.name + ".restore-tmp")).exists()


def test_restore_refuses_active_wal_sidecar(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.db")
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db"
    create_plaintext_backup(source, backup)
    target = tmp_path / "live" / "heartlog.db"
    target.parent.mkdir()
    target.write_bytes(b"")
    Path(str(target) + "-wal").write_bytes(b"active wal contents")

    with pytest.raises(BackupError, match="sidecar"):
        restore.restore_backup(backup, target, force=True)


# ---------------------------------------------------------------------------
# Restore drill
# ---------------------------------------------------------------------------


def test_restore_drill_passes_for_good_plaintext_and_encrypted_backups(
    client,
    app,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_heartlog_db(client, app)
    source = _configured_source_db(settings)
    backup_dir = tmp_path / "backups"
    drill_temp_before = set(Path(tempfile.gettempdir()).glob("heartlog-drill-*"))

    plain = backup_dir / "heartlog-20260101T000000Z.db"
    create_plaintext_backup(source, plain)
    plain_report = drill_backup(plain)
    assert plain_report["ok"] is True
    assert plain_report["issues"] == []
    check_names = {check["name"] for check in plain_report["checks"]}
    assert {
        "checksum",
        "integrity_check",
        "schema_tables",
        "diary_original_hashes",
        "diary_revision_chains",
        "memory_sources",
        "memory_evidence",
    } <= check_names
    assert plain_report["table_counts"]["diary_entries"] == 1

    encrypted = backup_dir / "heartlog-20260102T000000Z.db.enc"
    create_encrypted_backup(source, encrypted, PASSPHRASE)
    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, PASSPHRASE)
    encrypted_report = drill_backup(encrypted)
    assert encrypted_report["ok"] is True
    assert encrypted_report["encrypted"] is True

    assert restore_drill.main([str(encrypted), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert PASSPHRASE not in captured.out
    assert PRIVATE_MARKER not in captured.out
    assert set(Path(tempfile.gettempdir()).glob("heartlog-drill-*")) == drill_temp_before


def test_legacy_mvp_manifest_backup_is_still_usable(
    client,
    app,
    settings,
    tmp_path: Path,
) -> None:
    """Backups produced by the pre-productization MVP stay drillable/restorable."""

    _seed_heartlog_db(client, app)
    source = _configured_source_db(settings)
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db"
    create_plaintext_backup(source, backup)
    current = read_manifest(backup)
    assert current is not None
    legacy_manifest = {
        "app_version": current["app_version"],
        "created_at": current["created_at"],
        "source": current["source"],
        "backup": current["backup"],
        "sha256": current["sha256"],
        "size_bytes": current["size_bytes"],
        "integrity_check": current["integrity_check"],
        "table_counts": current["table_counts"],
    }
    manifest_path(backup).write_text(json.dumps(legacy_manifest), encoding="utf-8")

    report = drill_backup(backup)
    assert report["ok"] is True
    assert report["encrypted"] is False

    target = tmp_path / "restored" / "heartlog.db"
    result = restore.restore_backup(backup, target)
    assert result["encrypted"] is False
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_restore_drill_detects_diary_and_memory_tampering(
    client,
    app,
    settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_heartlog_db(client, app)
    source = _configured_source_db(settings)
    backup = tmp_path / "backups" / "heartlog-20260101T000000Z.db"
    create_plaintext_backup(source, backup)

    connection = sqlite3.connect(backup)
    try:
        # Simulate an attacker who also rebuilt the checksum/manifest: drop the
        # database guards in the copy, then alter immutable content.
        connection.execute("DROP TRIGGER IF EXISTS trg_diary_revisions_immutable")
        connection.execute("DROP TRIGGER IF EXISTS trg_diary_revision_identity_immutable")
        connection.execute("UPDATE diary_revisions SET content = content || 'tampered'")
        connection.execute("DROP TRIGGER IF EXISTS trg_memory_sources_immutable")
        connection.execute("UPDATE memory_sources SET evidence_text = 'not in the revision'")
        connection.commit()
    finally:
        connection.close()
    _rewrite_metadata(backup)

    report = drill_backup(backup)
    assert report["ok"] is False
    assert any("revision_hash_mismatch" in issue for issue in report["issues"])
    assert any("evidence_not_in_revision" in issue for issue in report["issues"])
    assert PRIVATE_MARKER not in json.dumps(report)

    assert restore_drill.main([str(backup), "--json"]) == 1


def test_restore_drill_detects_encrypted_tamper_and_passphrase_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _make_source_db(tmp_path / "source.db")
    encrypted = tmp_path / "backups" / "heartlog-20260101T000000Z.db.enc"
    create_encrypted_backup(source, encrypted, PASSPHRASE)
    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, PASSPHRASE)

    # Wrong passphrase: clear error, no decrypted output, non-zero exit.
    wrong_report = drill_backup(encrypted, passphrase="wrong-passphrase-value")
    assert wrong_report["ok"] is False
    assert "decryption_failed" in wrong_report["issues"]

    monkeypatch.delenv(DEFAULT_PASSPHRASE_ENV)
    missing_report = drill_backup(encrypted)
    assert missing_report["ok"] is False
    assert "passphrase_missing" in missing_report["issues"]
    assert restore_drill.main([str(encrypted), "--json"]) == 1
    capsys.readouterr()

    tampered = bytearray(encrypted.read_bytes())
    tampered[-1] ^= 0xFF
    encrypted.write_bytes(bytes(tampered))
    tamper_report = drill_backup(encrypted, passphrase=PASSPHRASE)
    assert tamper_report["ok"] is False
    assert "verification_failed" in tamper_report["issues"]
