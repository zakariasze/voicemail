"""SQLite-backed call outcome log.

One row per Twilio ``CallSid``. Written by ``call_handler``'s
``/voice`` and ``/status`` endpoints; read by the Phase 5 dashboard.

Schema (table ``calls``):

* ``call_sid``    TEXT PRIMARY KEY — Twilio's identifier
* ``to_number``   TEXT             — E.164 destination
* ``outcome``     TEXT             — one of the README outcomes
                                     (``Voicemail Left``, ``Human Answered``,
                                     ``No Answer``, ``Busy``, ``Failed``)
* ``answered_by`` TEXT             — raw Twilio ``AnsweredBy`` value
* ``call_status`` TEXT             — raw Twilio ``CallStatus`` value
* ``created_at``  TEXT (ISO 8601, UTC)
* ``updated_at``  TEXT (ISO 8601, UTC)

The module is deliberately stdlib-only.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

# README outcomes — kept here as the single source of truth so other
# modules can import and compare against them.
OUTCOME_VOICEMAIL_LEFT = "Voicemail Left"
OUTCOME_HUMAN_ANSWERED = "Human Answered"
OUTCOME_NO_ANSWER = "No Answer"
OUTCOME_BUSY = "Busy"
OUTCOME_FAILED = "Failed"

ALL_OUTCOMES = {
    OUTCOME_VOICEMAIL_LEFT,
    OUTCOME_HUMAN_ANSWERED,
    OUTCOME_NO_ANSWER,
    OUTCOME_BUSY,
    OUTCOME_FAILED,
}


def db_path() -> str:
    """Path to the SQLite file. Override with ``VOICEMAIL_DB_PATH``."""
    return os.environ.get("VOICEMAIL_DB_PATH", "voicemail.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_sid    TEXT PRIMARY KEY,
    to_number   TEXT,
    outcome     TEXT,
    answered_by TEXT,
    call_status TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    try:
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema if it doesn't exist. Safe to call repeatedly."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_outcome(
    call_sid: str,
    *,
    to_number: str | None = None,
    outcome: str | None = None,
    answered_by: str | None = None,
    call_status: str | None = None,
) -> None:
    """Upsert a row keyed by ``call_sid``.

    Only non-``None`` fields are written, so a later partial update
    (e.g. from the ``/status`` callback) cannot blank out an outcome
    set earlier by ``/voice``.
    """
    if not call_sid:
        raise ValueError("call_sid is required")
    if outcome is not None and outcome not in ALL_OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")

    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT call_sid, to_number, outcome, answered_by, call_status "
            "FROM calls WHERE call_sid = ?",
            (call_sid,),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO calls "
                "(call_sid, to_number, outcome, answered_by, call_status, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    call_sid,
                    to_number,
                    outcome,
                    answered_by,
                    call_status,
                    now,
                    now,
                ),
            )
            return

        new_to = to_number if to_number is not None else row["to_number"]
        # Don't overwrite an existing outcome with None.
        new_outcome = outcome if outcome is not None else row["outcome"]
        new_answered = (
            answered_by if answered_by is not None else row["answered_by"]
        )
        new_status = call_status if call_status is not None else row["call_status"]

        conn.execute(
            "UPDATE calls SET to_number=?, outcome=?, answered_by=?, "
            "call_status=?, updated_at=? WHERE call_sid=?",
            (new_to, new_outcome, new_answered, new_status, now, call_sid),
        )


def get(call_sid: str) -> dict | None:
    """Return the row for ``call_sid`` as a dict, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        return dict(row) if row else None


def list_recent(limit: int = 50) -> list[dict]:
    """Most-recently-updated calls first. Used for verification + Phase 5."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tiny self-test: `python state.py` round-trips the schema in a temp DB and
# prints PASS. Not a substitute for the manual verification in
# docs/phase2-plan.md, but a sanity check that the module imports and the
# SQL is well-formed.
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["VOICEMAIL_DB_PATH"] = os.path.join(tmp, "test.db")
        init_db()

        # Voice arrives first: machine -> Voicemail Left
        record_outcome(
            "CA1",
            to_number="+15550000001",
            outcome=OUTCOME_VOICEMAIL_LEFT,
            answered_by="machine_end_beep",
        )
        # Status arrives later: completed. Must NOT overwrite outcome.
        record_outcome("CA1", call_status="completed")
        row = get("CA1")
        assert row is not None
        assert row["outcome"] == OUTCOME_VOICEMAIL_LEFT, row
        assert row["call_status"] == "completed", row
        assert row["answered_by"] == "machine_end_beep", row

        # No-answer path: only /status fires.
        record_outcome(
            "CA2",
            to_number="+15550000002",
            outcome=OUTCOME_NO_ANSWER,
            call_status="no-answer",
        )
        row = get("CA2")
        assert row["outcome"] == OUTCOME_NO_ANSWER, row

        # Unknown outcome must raise.
        try:
            record_outcome("CA3", outcome="Bogus")
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("unknown outcome should have raised")

        assert len(list_recent()) == 2
        print("state.py self-test PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
