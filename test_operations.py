"""Backup script unit tests and audit-privacy checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from scripts.backup import backup_database, sha256_file


def test_backup_creates_verified_copy_and_manifest(settings, tmp_path: Path) -> None:
    # Create a real database through the app fixture infrastructure by using the
    # configured test database path; the settings fixture points at tmp_path.
    db_path = Path(make_url(settings.database_url).database or "")
    # The database may not exist yet; create a minimal valid SQLite DB for this
    # script-level test.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO sample (note) VALUES ('private note')")
        connection.commit()
    finally:
        connection.close()

    destination = tmp_path / "backups" / "copy.db"
    manifest = backup_database(db_path, destination)
    assert destination.exists()
    assert manifest["integrity_check"] == "ok"
    assert manifest["sha256"] == sha256_file(destination)
    assert destination.with_suffix(destination.suffix + ".sha256").exists()
    manifest_file = destination.with_suffix(destination.suffix + ".json")
    assert json.loads(manifest_file.read_text(encoding="utf-8"))["sha256"] == manifest["sha256"]
    with pytest.raises(SystemExit):
        backup_database(db_path, destination)


def test_update_audit_does_not_copy_diary_text(client, diary_factory, db_session) -> None:
    from app.models.audit import AuditEvent

    secret_text = "这是一段不应出现在审计记录里的完整私人日记内容。"
    diary = diary_factory(secret_text)
    client.put(f"/api/v1/diaries/{diary['id']}", json={"content": "修改后的私人内容也不应出现在审计中。"})
    events = db_session.query(AuditEvent).all()
    serialized = json.dumps([event.details for event in events], ensure_ascii=False)
    assert "不应出现在审计记录里" not in serialized
    assert "修改后的私人内容" not in serialized
    assert events, "audit events should exist"
