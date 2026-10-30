"""Migration smoke test: upgrade head on an isolated database.

The test runs Alembic in-process with an explicit temporary SQLite URL so it can
never touch the user's real ``data/heartlog.db``.  In-process execution also
avoids platform-specific subprocess-pipe restrictions in the sandbox.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head_creates_schema_and_guards(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("HEARTLOG_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("HEARTLOG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HEARTLOG_BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    try:
        alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        command.upgrade(alembic_config, "head")
    finally:
        get_settings.cache_clear()

    assert db_path.exists()
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        triggers = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()
    assert {
        "diary_entries",
        "diary_revisions",
        "memories",
        "memory_sources",
        "persons",
        "audit_events",
        "conversations",
        "messages",
    } <= tables
    assert "trg_diary_original_text_immutable" in triggers
    assert "trg_audit_events_no_update" in triggers
    assert version is not None
