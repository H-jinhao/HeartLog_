"""Project-wide SQLite transaction semantics.

These tests protect the canonical pysqlite BEGIN fix in app/db/session.py.  A
deferred BEGIN can make RELEASE of an early SAVEPOINT commit the whole outer
transaction, which would break atomic multi-step writes and rollbacks.
"""

from __future__ import annotations

from sqlalchemy import text

from app.models.diary import DiaryEntry


def test_early_savepoint_release_does_not_commit_outer_transaction(app) -> None:
    factory = app.state.session_factory
    session = factory()
    try:
        # SELECT starts the SQLAlchemy transaction.  With the canonical BEGIN
        # listener the DBAPI transaction now exists before the SAVEPOINT.
        session.execute(text("SELECT 1"))
        with session.begin_nested():
            session.add(DiaryEntry(original_text="savepoint entry"))
            session.flush()
        # The savepoint released without committing the outer transaction.
        session.rollback()
    finally:
        session.close()

    verify = factory()
    try:
        count = verify.query(DiaryEntry).count()
        assert count == 0, "outer rollback must undo a released savepoint"
    finally:
        verify.close()


def test_work_committed_after_savepoint_is_persisted(app) -> None:
    factory = app.state.session_factory
    session = factory()
    try:
        with session.begin_nested():
            session.add(DiaryEntry(original_text="committed entry"))
            session.flush()
        session.commit()
    finally:
        session.close()
    verify = factory()
    try:
        assert verify.query(DiaryEntry).count() == 1
    finally:
        verify.close()
