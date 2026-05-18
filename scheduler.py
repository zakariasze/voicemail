"""Outbound voicemail-drop scheduler.

Iterates the configured HubSpot list, filters out contacts that have
already had two attempts or have no usable phone, and places a Twilio
call for each remaining contact with a fixed delay between placements.

This is a deliberately simple Phase 4 implementation. The README's
ultimate goal is time-zone-aware business-hours windows (10:00–11:30
AM and 2:00–3:30 PM local, two attempts per contact). With only two
test contacts in the list right now, the user has asked for a fixed
N-second interval instead — see ``docs/phase4-plan.md`` for the
deferral rationale and the migration path to real windows.

Public surface
--------------
* :data:`MAX_ATTEMPTS` — the hard cap (2, per README).
* :func:`count_attempts(contact_id)` — read SQLite, return the number
  of placements already made for ``contact_id``.
* :func:`pending_contacts(contacts)` — filter a list of HubSpot
  contacts down to the ones that should be dialled next.
* :func:`run_once(interval_seconds=None, *, dry_run=False)` — execute
  one scheduler pass.

CLI
---
::

    python scheduler.py                      # one pass, default interval
    python scheduler.py --dry-run            # list only, place no calls
    python scheduler.py --interval 10        # one pass, 10s between calls
    python scheduler.py --loop               # repeat run_once forever
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Iterable

import hubspot_client
import state
import twilio_client

# Per README "Phase 4 — Scheduler": two attempts per contact.
MAX_ATTEMPTS = 2

# Default seconds between placements when neither --interval nor
# CALL_INTERVAL_SECONDS is set. 5s matches the user's Phase-4 brief.
_DEFAULT_INTERVAL_SECONDS = 5

# Seconds between successive run_once passes under --loop.
_DEFAULT_LOOP_INTERVAL_SECONDS = 60


# --- Attempt counting ------------------------------------------------------

def count_attempts(contact_id: str) -> int:
    """Return how many placements have been recorded for ``contact_id``.

    Reads the ``calls`` table written by ``twilio_client.place_call``
    (via ``state.record_call_placed``). Counts placements, not
    completed outcomes — see ``docs/phase4-plan.md`` §1.
    """
    if not contact_id:
        return 0
    conn = sqlite3.connect(state.db_path())
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE hubspot_contact_id = ?",
            (str(contact_id),),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# --- Filtering -------------------------------------------------------------

def pending_contacts(
    contacts: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Split ``contacts`` into ``(to_call, skipped_with_reason)``.

    A contact is *to_call* when:
    * it has an ``id``;
    * its ``phone`` normalises to E.164 successfully;
    * fewer than :data:`MAX_ATTEMPTS` placements exist for it.

    Each skipped contact is paired with a short human-readable reason
    so the CLI can print it.
    """
    to_call: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], str]] = []

    for c in contacts:
        cid = c.get("id")
        if not cid:
            skipped.append((c, "no contact id"))
            continue
        phone = hubspot_client.normalize_phone(c.get("phone"))
        if not phone:
            skipped.append((c, f"no usable phone (raw={c.get('phone')!r})"))
            continue
        attempts = count_attempts(cid)
        if attempts >= MAX_ATTEMPTS:
            skipped.append((c, f"{attempts} attempts already made"))
            continue
        # Attach the normalised phone so run_once doesn't redo the work.
        c = {**c, "_phone_e164": phone, "_attempts_before": attempts}
        to_call.append(c)

    return to_call, skipped


# --- Scheduling pass -------------------------------------------------------

def _resolve_interval(interval_seconds: int | None) -> int:
    if interval_seconds is not None:
        return max(0, int(interval_seconds))
    raw = os.environ.get("CALL_INTERVAL_SECONDS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            print(
                f"[scheduler] WARNING: CALL_INTERVAL_SECONDS={raw!r} is not "
                f"an integer; falling back to {_DEFAULT_INTERVAL_SECONDS}s",
                flush=True,
            )
    return _DEFAULT_INTERVAL_SECONDS


def _describe(contact: dict[str, Any]) -> str:
    name = " ".join(
        part for part in (contact.get("firstname"), contact.get("lastname")) if part
    ).strip() or "(no name)"
    return f"contact {contact.get('id')} ({name})"


def run_once(
    interval_seconds: int | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Execute one scheduler pass.

    Returns a ``{"dialed": int, "skipped": int}`` summary so callers
    (and ``--loop``) can report progress.
    """
    interval = _resolve_interval(interval_seconds)

    contacts = hubspot_client.list_contacts()
    to_call, skipped = pending_contacts(contacts)

    for c, reason in skipped:
        print(f"[scheduler] skip {_describe(c)}: {reason}", flush=True)

    if not to_call:
        print(
            f"[scheduler] 0 contact(s) dialed, {len(skipped)} skipped",
            flush=True,
        )
        return {"dialed": 0, "skipped": len(skipped)}

    if dry_run:
        for c in to_call:
            print(
                f"[scheduler] would call {_describe(c)} -> {c['_phone_e164']} "
                f"(attempt {c['_attempts_before'] + 1} of {MAX_ATTEMPTS})",
                flush=True,
            )
        print(
            f"[scheduler] {len(to_call)} contact(s) would be dialed, "
            f"{len(skipped)} skipped",
            flush=True,
        )
        return {"dialed": 0, "skipped": len(skipped)}

    dialed = 0
    for i, c in enumerate(to_call):
        print(
            f"[scheduler] dialing {_describe(c)} -> {c['_phone_e164']} "
            f"(attempt {c['_attempts_before'] + 1} of {MAX_ATTEMPTS})",
            flush=True,
        )
        try:
            twilio_client.place_call(
                c["_phone_e164"],
                hubspot_contact_id=str(c["id"]),
            )
            dialed += 1
        except Exception as exc:  # noqa: BLE001 - never crash a whole pass
            print(
                f"[scheduler] ERROR placing call for {_describe(c)}: {exc}",
                flush=True,
            )
        if interval > 0 and i < len(to_call) - 1:
            time.sleep(interval)

    print(
        f"[scheduler] {dialed} contact(s) dialed, {len(skipped)} skipped",
        flush=True,
    )
    return {"dialed": dialed, "skipped": len(skipped)}


# --- CLI -------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Voicemail-drop scheduler (Phase 4).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the contacts that would be dialled but do not call.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Seconds between successive call placements. "
            f"Default: $CALL_INTERVAL_SECONDS or "
            f"{_DEFAULT_INTERVAL_SECONDS}."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Re-run forever, sleeping --loop-interval seconds between passes."
        ),
    )
    parser.add_argument(
        "--loop-interval",
        type=int,
        default=_DEFAULT_LOOP_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=(
            f"Seconds between passes when --loop is set. "
            f"Default: {_DEFAULT_LOOP_INTERVAL_SECONDS}."
        ),
    )
    return parser


def _main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv[1:])
    if args.loop:
        while True:
            run_once(args.interval, dry_run=args.dry_run)
            time.sleep(max(0, int(args.loop_interval)))
    else:
        run_once(args.interval, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
