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

CREATE TABLE IF NOT EXISTS campaigns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'paused'
               CHECK (status IN ('active', 'paused', 'done')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name        TEXT,
    phone       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaign_contacts_campaign_id
    ON campaign_contacts(campaign_id);
"""

# Campaign status constants — single source of truth.
CAMPAIGN_ACTIVE = "active"
CAMPAIGN_PAUSED = "paused"
CAMPAIGN_DONE = "done"
CAMPAIGN_STATUSES = {CAMPAIGN_ACTIVE, CAMPAIGN_PAUSED, CAMPAIGN_DONE}


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
# Phase 5: campaigns
# ---------------------------------------------------------------------------

def create_campaign(name: str) -> int:
    """Create a new campaign in ``paused`` state. Returns its id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("campaign name is required")
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, CAMPAIGN_PAUSED, now, now),
        )
        return int(cur.lastrowid)


def list_campaigns() -> list[dict]:
    """All campaigns, newest first, with a ``contact_count`` field."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.status, c.created_at, c.updated_at, "
            "       COUNT(cc.id) AS contact_count "
            "FROM campaigns c "
            "LEFT JOIN campaign_contacts cc ON cc.campaign_id = c.id "
            "GROUP BY c.id "
            "ORDER BY c.created_at DESC",
        ).fetchall()
        return [dict(r) for r in rows]


def get_campaign(campaign_id: int) -> dict | None:
    """Return one campaign by id, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),),
        ).fetchone()
        return dict(row) if row else None


def set_campaign_status(campaign_id: int, status: str) -> None:
    """Manually update a campaign's status. Validates against the enum."""
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"unknown campaign status: {status!r}")
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
            (status, now, int(campaign_id)),
        )


def add_campaign_contacts(
    campaign_id: int,
    contacts: list[dict],
) -> int:
    """Bulk-insert ``contacts`` (already-normalised) into a campaign.

    Each row must have a non-empty ``phone``. ``name`` is optional.
    Returns the number of rows inserted. Caller is responsible for
    phone normalisation (use ``hubspot_client.normalize_phone``).
    """
    if not contacts:
        return 0
    now = _now()
    rows = []
    for c in contacts:
        phone = (c.get("phone") or "").strip()
        if not phone:
            continue
        rows.append((int(campaign_id), (c.get("name") or None), phone, now))
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO campaign_contacts "
            "(campaign_id, name, phone, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def list_campaign_contacts(campaign_id: int) -> list[dict]:
    """Contacts in a campaign with per-phone call stats joined in.

    Returns rows shaped like::

        {
            "id": int, "name": str|None, "phone": str,
            "attempt_count": int,
            "last_outcome": str|None,
            "last_call_at": str|None,  # ISO 8601 UTC from calls.updated_at
        }

    Stats come from a LEFT JOIN on ``calls.to_number = campaign_contacts.phone``.
    A contact with no calls has ``attempt_count=0`` and ``None`` for the
    other two fields.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT "
            "  cc.id, cc.name, cc.phone, "
            "  COUNT(c.call_sid) AS attempt_count, "
            "  MAX(c.updated_at) AS last_call_at, "
            "  ( "
            "    SELECT c2.outcome FROM calls c2 "
            "    WHERE c2.to_number = cc.phone AND c2.outcome IS NOT NULL "
            "    ORDER BY c2.updated_at DESC LIMIT 1 "
            "  ) AS last_outcome "
            "FROM campaign_contacts cc "
            "LEFT JOIN calls c ON c.to_number = cc.phone "
            "WHERE cc.campaign_id = ? "
            "GROUP BY cc.id "
            "ORDER BY cc.id ASC",
            (int(campaign_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def list_active_campaign_targets() -> list[dict]:
    """Phones to dial from currently-active campaigns, in insertion order.

    Used by the scheduler. Returns rows shaped like::

        {"campaign_id": int, "campaign_contact_id": int,
         "name": str|None, "phone": str}

    Only rows whose campaign has ``status='active'`` are returned.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cc.campaign_id, cc.id AS campaign_contact_id, "
            "       cc.name, cc.phone "
            "FROM campaign_contacts cc "
            "JOIN campaigns c ON c.id = cc.campaign_id "
            "WHERE c.status = ? "
            "ORDER BY cc.campaign_id ASC, cc.id ASC",
            (CAMPAIGN_ACTIVE,),
        ).fetchall()
        return [dict(r) for r in rows]


def has_active_campaign() -> bool:
    """``True`` iff at least one campaign has ``status='active'``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM campaigns WHERE status = ? LIMIT 1",
            (CAMPAIGN_ACTIVE,),
        ).fetchone()
        return row is not None


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

        # ------------------------------------------------------------------
        # Phase 5: campaigns
        # ------------------------------------------------------------------
        cid = create_campaign("smoke")
        assert isinstance(cid, int) and cid > 0

        # Newly created campaigns are paused, not active.
        camp = get_campaign(cid)
        assert camp is not None
        assert camp["status"] == CAMPAIGN_PAUSED, camp
        assert not has_active_campaign()

        # Insert two contacts; one with a phone matching an existing call.
        inserted = add_campaign_contacts(
            cid,
            [
                {"name": "Alice", "phone": "+15550000001"},  # has 1 call (CA1)
                {"name": "Bob",   "phone": "+15559999999"},  # no calls
                {"name": "",      "phone": ""},              # skipped
            ],
        )
        assert inserted == 2, inserted

        rows = list_campaign_contacts(cid)
        assert len(rows) == 2, rows
        by_phone = {r["phone"]: r for r in rows}
        assert by_phone["+15550000001"]["attempt_count"] == 1
        assert by_phone["+15550000001"]["last_outcome"] == OUTCOME_VOICEMAIL_LEFT
        assert by_phone["+15550000001"]["last_call_at"] is not None
        assert by_phone["+15559999999"]["attempt_count"] == 0
        assert by_phone["+15559999999"]["last_outcome"] is None
        assert by_phone["+15559999999"]["last_call_at"] is None

        # Active targets: empty while paused, populated when active.
        assert list_active_campaign_targets() == []
        set_campaign_status(cid, CAMPAIGN_ACTIVE)
        assert has_active_campaign()
        targets = list_active_campaign_targets()
        assert len(targets) == 2
        assert {t["phone"] for t in targets} == {"+15550000001", "+15559999999"}

        # Manual transitions.
        set_campaign_status(cid, CAMPAIGN_PAUSED)
        assert not has_active_campaign()
        set_campaign_status(cid, CAMPAIGN_DONE)
        assert get_campaign(cid)["status"] == CAMPAIGN_DONE

        # Bad status must raise.
        try:
            set_campaign_status(cid, "bogus")
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("bad status should have raised")

        # Empty-name campaign must raise.
        try:
            create_campaign("   ")
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("empty name should have raised")

        # list_campaigns includes contact_count.
        campaigns = list_campaigns()
        assert len(campaigns) == 1
        assert campaigns[0]["contact_count"] == 2

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
