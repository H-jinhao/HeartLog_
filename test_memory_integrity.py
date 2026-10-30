"""Integrity-guard tests: ORM immutability, SQLite triggers, review constraints.

These verify defence-in-depth around the memory workflow: even code (or raw
SQL) that bypasses the service cannot rewrite evidence, rewrite a memory's
content/type, mutate audit events, or confirm a memory without a named reviewer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ProtectedRecordError
from app.models.audit import AuditEvent
from app.models.memory import Memory, MemorySource
from tests.test_memories import (
    EVIDENCE_HAPPY,
    candidate,
    make_diary_entry,
    refresh,
    run_extraction,
)


def _create_pending_memory(client, app, db_session) -> str:
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    assert response.status_code == 200, response.text
    return response.json()["created"][0]["id"]


# ---------------------------------------------------------------------------
# ORM-level immutability
# ---------------------------------------------------------------------------
def test_memory_content_cannot_be_rewritten_via_orm(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    refresh(db_session)
    memory = db_session.get(Memory, memory_id)
    assert memory is not None
    memory.content = "彻底改写后的内容"

    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()

    refresh(db_session)
    stored = db_session.get(Memory, memory_id)
    assert stored is not None
    assert stored.content == "她回复了我的消息"


def test_memory_type_cannot_be_changed_via_orm(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    refresh(db_session)
    memory = db_session.get(Memory, memory_id)
    assert memory is not None
    memory.memory_type = "explicit_belief"

    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()


def test_memory_sources_are_immutable_via_orm(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    refresh(db_session)
    source = db_session.scalar(select(MemorySource).where(MemorySource.memory_id == memory_id))
    assert source is not None
    source.evidence_text = "替换后的伪证据"

    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()


def test_audit_events_are_append_only_via_orm(client, app, db_session):
    _create_pending_memory(client, app, db_session)
    refresh(db_session)
    event = db_session.scalars(select(AuditEvent)).first()
    assert event is not None
    event.details = {"tampered": True}
    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()

    refresh(db_session)
    event = db_session.scalars(select(AuditEvent)).first()
    assert event is not None
    db_session.delete(event)
    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Raw SQL defence in depth (SQLite triggers / CHECK constraints)
# ---------------------------------------------------------------------------
def test_raw_sql_cannot_rewrite_memory_content(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    with pytest.raises(IntegrityError):
        with app.state.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE memories SET content = ? WHERE id = ?",
                ("raw sql tampering", memory_id),
            )


def test_raw_sql_cannot_rewrite_memory_source_evidence(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    with pytest.raises(IntegrityError):
        with app.state.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE memory_sources SET evidence_text = ? WHERE memory_id = ?",
                ("raw sql tampering", memory_id),
            )


def test_raw_sql_cannot_mutate_audit_events(client, app, db_session):
    _create_pending_memory(client, app, db_session)
    with pytest.raises(IntegrityError):
        with app.state.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE audit_events SET actor = 'model'")
    with pytest.raises(IntegrityError):
        with app.state.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM audit_events")


def test_raw_confirmation_requires_named_reviewer(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    with pytest.raises(IntegrityError):
        with app.state.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE memories SET verification_status = 'confirmed' WHERE id = ?",
                (memory_id,),
            )


def test_inactive_constraints_are_enforced_by_database(client, app, db_session):
    memory_id = _create_pending_memory(client, app, db_session)
    for invalid_status in ("revoked", "superseded"):
        with pytest.raises(IntegrityError):
            with app.state.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE memories SET verification_status = ? WHERE id = ?",
                    (invalid_status, memory_id),
                )


# ---------------------------------------------------------------------------
# No source, no memory
# ---------------------------------------------------------------------------
def test_source_less_memory_insert_is_rejected_by_database(db_session):
    memory = Memory(
        content="没有来源的记忆",
        memory_type="explicit_fact",
        source_type="diary_entry",
        source_entry_id=None,
        source_revision_id=None,
        created_by="model",
    )
    db_session.add(memory)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
