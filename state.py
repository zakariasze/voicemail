"""SQLite-backed call outcome log.

One row per Twilio ``CallSid``. Written by ``call_handler``'s
``/voice`` and ``/status`` endpoints; read by the Phase 5 dashboard.

Schema (table ``calls``):

* ``call_sid``            TEXT PRIMARY KEY — Twilio's identifier
* ``to_number``           TEXT             — E.164 destination
* ``outcome``             TEXT             — one of the README outcomes
                                             (``Voicemail Left``,
                                             ``Human Answered``,
                                             ``No Answer``, ``Busy``,
                                             ``Failed``)
* ``answered_by``         TEXT             — raw Twilio ``AnsweredBy``
* ``call_status``         TEXT             — raw Twilio ``CallStatus``
* ``hubspot_contact_id``  TEXT             — set at call placement
                                             (Phase 3)
* ``hubspot_logged_at``   TEXT (ISO 8601)  — set once the outcome has
                                             been pushed to HubSpot
* ``created_at``          TEXT (ISO 8601, UTC)
* ``updated_at``          TEXT (ISO 8601, UTC)

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
    call_sid           TEXT PRIMARY KEY,
    to_number          TEXT,
    outcome            TEXT,
    answered_by        TEXT,
    call_status        TEXT,
    hubspot_contact_id TEXT,
    hubspot_logged_at  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
"""


# Columns introduced after the original schema. Added at init time via
# ``ALTER TABLE ... ADD COLUMN`` if missing, so existing Phase 2 databases
# migrate forward without losing rows.
_MIGRATIONS: list[tuple[str, str]] = [
    ("hubspot_contact_id", "ALTER TABLE calls ADD COLUMN hubspot_contact_id TEXT"),
    ("hubspot_logged_at",  "ALTER TABLE calls ADD COLUMN hubspot_logged_at TEXT"),
]


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
    """Create the schema if it doesn't exist. Safe to call repeatedly.

    Also applies additive column migrations for databases originally
    created on an older schema.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(calls)")}
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_outcome(
    call_sid: str,
    *,
    to_number: str | None = None,
    outcome: str | None = None,
    answered_by: str | None = None,
    call_status: str | None = None,
    hubspot_contact_id: str | None = None,
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
            "SELECT call_sid, to_number, outcome, answered_by, call_status, "
            "hubspot_contact_id FROM calls WHERE call_sid = ?",
            (call_sid,),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO calls "
                "(call_sid, to_number, outcome, answered_by, call_status, "
                " hubspot_contact_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_sid,
                    to_number,
                    outcome,
                    answered_by,
                    call_status,
                    hubspot_contact_id,
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
        new_contact = (
            hubspot_contact_id
            if hubspot_contact_id is not None
            else row["hubspot_contact_id"]
        )

        conn.execute(
            "UPDATE calls SET to_number=?, outcome=?, answered_by=?, "
            "call_status=?, hubspot_contact_id=?, updated_at=? "
            "WHERE call_sid=?",
            (
                new_to,
                new_outcome,
                new_answered,
                new_status,
                new_contact,
                now,
                call_sid,
            ),
        )


def record_call_placed(
    call_sid: str,
    *,
    to_number: str,
    hubspot_contact_id: str | None = None,
) -> None:
    """Record that an outbound call has been placed.

    Creates a row with ``to_number`` and (optionally) ``hubspot_contact_id``
    so that later webhook hits can find the contact by ``CallSid``. The
    outcome columns are left ``NULL`` until the webhooks fill them in.
    """
    record_outcome(
        call_sid,
        to_number=to_number,
        hubspot_contact_id=hubspot_contact_id,
    )


def mark_hubspot_logged(call_sid: str) -> None:
    """Stamp ``hubspot_logged_at`` so we don't double-log on Twilio retries."""
    if not call_sid:
        raise ValueError("call_sid is required")
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE calls SET hubspot_logged_at=?, updated_at=? "
            "WHERE call_sid=?",
            (now, now, call_sid),
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

        # Phase 3: placement records the contact id up front.
        record_call_placed(
            "CA1",
            to_number="+15550000001",
            hubspot_contact_id="C-1",
        )
        # Voice arrives first: machine -> Voicemail Left. Must NOT clobber
        # the contact id set at placement time.
        record_outcome(
            "CA1",
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
        assert row["hubspot_contact_id"] == "C-1", row
        assert row["hubspot_logged_at"] is None, row

        # HubSpot push happens -> stamp must land.
        mark_hubspot_logged("CA1")
        assert get("CA1")["hubspot_logged_at"] is not None

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

        # Migration round-trip: create an old-schema DB (no Phase 3
        # columns) then re-init and confirm the columns appear and the
        # original row survives.
        old_db = os.path.join(tmp, "old.db")
        os.environ["VOICEMAIL_DB_PATH"] = old_db
        import sqlite3 as _sql
        c = _sql.connect(old_db)
        c.executescript(
            "CREATE TABLE calls ("
            " call_sid TEXT PRIMARY KEY, to_number TEXT, outcome TEXT,"
            " answered_by TEXT, call_status TEXT,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO calls VALUES "
            "('OLD','+1555','Voicemail Left','machine_end_beep',"
            "'completed','2024-01-01T00:00:00+00:00',"
            "'2024-01-01T00:00:00+00:00');"
        )
        c.commit()
        c.close()
        init_db()  # should migrate
        row = get("OLD")
        assert row is not None
        assert row["hubspot_contact_id"] is None
        assert row["hubspot_logged_at"] is None
        assert row["outcome"] == OUTCOME_VOICEMAIL_LEFT

        print("state.py self-test PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
