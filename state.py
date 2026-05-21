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
from datetime import datetime, timedelta, timezone
from typing import Iterator

# README outcomes — kept here as the single source of truth so other
# modules can import and compare against them.
OUTCOME_VOICEMAIL_LEFT = "Voicemail Left"
OUTCOME_HUMAN_ANSWERED = "Human Answered"
OUTCOME_TRANSFERRED = "Transferred"
OUTCOME_NO_ANSWER = "No Answer"
OUTCOME_BUSY = "Busy"
OUTCOME_FAILED = "Failed"

ALL_OUTCOMES = {
    OUTCOME_VOICEMAIL_LEFT,
    OUTCOME_HUMAN_ANSWERED,
    OUTCOME_TRANSFERRED,
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
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    hubspot_list_id  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'paused'
                     CHECK (status IN ('active', 'paused', 'done')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
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
    # Stamped by ``mark_sms_sent`` after the voicemail follow-up SMS is
    # placed with Twilio. Used to keep retries idempotent and to surface
    # the "VM + SMS" state on the dashboard.
    ("sms_sent_at",        "ALTER TABLE calls ADD COLUMN sms_sent_at TEXT"),
]

# Additive migrations for the campaigns table (added after the initial
# Phase 5 commit). Same idea as ``_MIGRATIONS`` for ``calls`` above.
_CAMPAIGN_MIGRATIONS: list[tuple[str, str]] = [
    ("hubspot_list_id",
     "ALTER TABLE campaigns ADD COLUMN hubspot_list_id TEXT NOT NULL DEFAULT ''"),
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
        existing_campaign = {
            row["name"] for row in conn.execute("PRAGMA table_info(campaigns)")
        }
        for column, ddl in _CAMPAIGN_MIGRATIONS:
            if column not in existing_campaign:
                conn.execute(ddl)
        # Older Phase-5 dev DBs may still have the now-unused
        # campaign_contacts table. Drop it so the schema stays tidy.
        conn.execute("DROP TABLE IF EXISTS campaign_contacts")


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


def mark_sms_sent(call_sid: str) -> None:
    """Stamp ``sms_sent_at`` so we don't double-send the voicemail SMS.

    Called after the follow-up SMS placed in ``/voice`` (for a voicemail
    outcome) has been accepted by Twilio. Twilio occasionally retries
    ``/voice`` for the same ``CallSid``; checking this column makes the
    send idempotent.
    """
    if not call_sid:
        raise ValueError("call_sid is required")
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE calls SET sms_sent_at=?, updated_at=? WHERE call_sid=?",
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

def create_campaign(name: str, hubspot_list_id: str) -> int:
    """Create a new campaign in ``paused`` state. Returns its id.

    A campaign is a (name, HubSpot list id) pair. Contacts are pulled
    live from HubSpot at display / dial time — we don't snapshot them.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("campaign name is required")
    list_id = (hubspot_list_id or "").strip()
    if not list_id:
        raise ValueError("hubspot_list_id is required")
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns "
            "(name, hubspot_list_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, list_id, CAMPAIGN_PAUSED, now, now),
        )
        return int(cur.lastrowid)


def list_campaigns() -> list[dict]:
    """All campaigns, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, hubspot_list_id, status, created_at, updated_at "
            "FROM campaigns ORDER BY created_at DESC",
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


def get_active_campaign() -> dict | None:
    """Return the oldest currently-active campaign, or ``None``.

    "Oldest" — by ``created_at`` — gives a stable choice if the user
    accidentally has more than one active campaign.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE status = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (CAMPAIGN_ACTIVE,),
        ).fetchone()
        return dict(row) if row else None


def has_active_campaign() -> bool:
    """``True`` iff at least one campaign has ``status='active'``."""
    return get_active_campaign() is not None


def phone_call_stats(phones: list[str]) -> dict[str, dict]:
    """Look up call stats for a batch of phone numbers.

    Returns a ``{phone: {"attempt_count": int, "last_outcome": str|None,
    "last_call_at": str|None, "in_progress": bool}}`` map. Phones not
    present in the ``calls`` table are returned with ``attempt_count=0``,
    ``in_progress=False`` and ``None`` for the other two fields.

    ``in_progress`` is ``True`` iff a row exists for that phone with
    ``outcome IS NULL`` and ``created_at`` within the last 5 minutes —
    i.e. the call has been placed but no terminal webhook has landed
    yet. The 5-minute cap protects against rows that get orphaned if
    Twilio never calls back (we'd otherwise show a spinner forever).
    """
    out: dict[str, dict] = {
        p: {
            "attempt_count": 0,
            "last_outcome": None,
            "last_call_at": None,
            "in_progress": False,
        }
        for p in phones if p
    }
    if not out:
        return out
    placeholders = ",".join("?" for _ in out)
    phone_tuple = tuple(out.keys())
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT to_number, "
            f"  COUNT(*) AS attempt_count, "
            f"  MAX(updated_at) AS last_call_at "
            f"FROM calls "
            f"WHERE to_number IN ({placeholders}) "
            f"GROUP BY to_number",
            phone_tuple,
        ).fetchall()
        for r in rows:
            d = out[r["to_number"]]
            d["attempt_count"] = int(r["attempt_count"] or 0)
            d["last_call_at"] = r["last_call_at"]
        # Last outcome: pick most-recent non-NULL outcome per phone.
        rows = conn.execute(
            f"SELECT to_number, outcome FROM calls "
            f"WHERE to_number IN ({placeholders}) AND outcome IS NOT NULL "
            f"ORDER BY updated_at DESC",
            phone_tuple,
        ).fetchall()
        seen: set[str] = set()
        for r in rows:
            phone = r["to_number"]
            if phone in seen:
                continue
            seen.add(phone)
            out[phone]["last_outcome"] = r["outcome"]
        # In-progress: any row with outcome NULL placed in the last 5
        # minutes. SQLite stores ISO 8601 UTC strings; lexical compare
        # works because the format is fixed-width and timezone-stable.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat(timespec="seconds")
        rows = conn.execute(
            f"SELECT DISTINCT to_number FROM calls "
            f"WHERE to_number IN ({placeholders}) "
            f"  AND outcome IS NULL "
            f"  AND created_at >= ?",
            phone_tuple + (cutoff,),
        ).fetchall()
        for r in rows:
            out[r["to_number"]]["in_progress"] = True
    return out


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
        # Phase 5: campaigns (HubSpot-list sourced)
        # ------------------------------------------------------------------
        cid = create_campaign("smoke", "12345")
        assert isinstance(cid, int) and cid > 0

        camp = get_campaign(cid)
        assert camp is not None
        assert camp["status"] == CAMPAIGN_PAUSED, camp
        assert camp["hubspot_list_id"] == "12345", camp
        assert not has_active_campaign()
        assert get_active_campaign() is None

        # phone_call_stats: phones with and without prior calls.
        stats = phone_call_stats(["+15550000001", "+15559999999", ""])
        assert stats["+15550000001"]["attempt_count"] == 1
        assert stats["+15550000001"]["last_outcome"] == OUTCOME_VOICEMAIL_LEFT
        assert stats["+15550000001"]["last_call_at"] is not None
        assert stats["+15550000001"]["in_progress"] is False
        assert stats["+15559999999"]["attempt_count"] == 0
        assert stats["+15559999999"]["last_outcome"] is None
        assert stats["+15559999999"]["in_progress"] is False
        assert "" not in stats

        # In-progress: a freshly-placed row with NULL outcome shows up
        # as in_progress=True for that phone.
        record_call_placed(
            "CA_LIVE", to_number="+15558887777", hubspot_contact_id="C-LIVE",
        )
        stats = phone_call_stats(["+15558887777"])
        assert stats["+15558887777"]["in_progress"] is True
        # Once an outcome lands, in_progress flips back to False.
        record_outcome("CA_LIVE", outcome=OUTCOME_VOICEMAIL_LEFT)
        stats = phone_call_stats(["+15558887777"])
        assert stats["+15558887777"]["in_progress"] is False
        assert stats["+15558887777"]["last_outcome"] == OUTCOME_VOICEMAIL_LEFT

        # Active campaign lookup.
        set_campaign_status(cid, CAMPAIGN_ACTIVE)
        assert has_active_campaign()
        active = get_active_campaign()
        assert active is not None and active["id"] == cid
        assert active["hubspot_list_id"] == "12345"

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

        # Empty name / list id must raise.
        for bad in (("   ", "1"), ("name", "  ")):
            try:
                create_campaign(*bad)
            except ValueError:
                pass
            else:  # pragma: no cover - defensive
                raise AssertionError(f"empty arg should have raised: {bad!r}")

        # list_campaigns.
        campaigns = list_campaigns()
        assert len(campaigns) == 1
        assert campaigns[0]["hubspot_list_id"] == "12345"

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
