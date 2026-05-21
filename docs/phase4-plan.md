# Phase 4 — Scheduler

> Scope per `README.md → Build Phases → Phase 4`:
> * `scheduler.py` — timezone-aware scheduling, two windows per
>   contact (10:00–11:30 AM, 2:00–3:30 PM local).
> * Two-attempt tracking in SQLite (don't re-call if both attempts done).
> * `main.py` that bootstraps the scheduler.
> * Verification: run with a small test list, calls go out at the right
>   local times.

## User modification accepted

> "I currently only have 2 contacts in my HubSpot list, so for now
>  schedule calls to go out 5 seconds apart instead of waiting for real
>  business-hours windows. The scheduler should still track two attempts
>  per contact in SQLite and skip contacts that have already had both
>  attempts."

So this phase delivers:
* **Two-attempt tracking** — kept exactly as specified.
* **Interval scheduling** — fixed N-second gap between placements,
  configurable via `--interval` or `CALL_INTERVAL_SECONDS`, default 5.
* **Time-zone / business-hours windows** — explicitly deferred. The
  scheduler is structured so that swapping the "interval" loop for a
  "wait until next local window" loop later is a one-function change
  (`_sleep_between_placements`). See "Why deferred" below.

## Files added (and only these)

| File | Role |
|---|---|
| `scheduler.py` | Pure scheduling logic + CLI. No webhook state — just iterates HubSpot list, filters, and calls `twilio_client.place_call`. |
| `main.py` | Entry point: `state.init_db()` → `hubspot_client.ensure_custom_properties()` → `scheduler.run_once()`. Optional `--loop`. |

No existing file is modified. `CALL_INTERVAL_SECONDS` is read directly
from `os.environ` in `scheduler.py` so we don't touch `config.py` or
`.env.example`.

## Decisions

### 1. Attempt counting source of truth
The SQLite `calls` table already records every placement (one row per
`CallSid`, with `hubspot_contact_id` set from Phase 3). "Attempts so
far for contact X" = `SELECT COUNT(*) FROM calls WHERE
hubspot_contact_id = X`. This is naturally correct across restarts
and matches the README's "local SQLite state table as the source of
truth (HubSpot is the log, not the scheduler state)".

We count *placements*, not *outcomes*. A call placement that errored
mid-Twilio-API still consumes an attempt — that's intentional, so a
bad-number contact doesn't get retried forever within a single run.

### 2. Skip rules
A contact is skipped (not dialled this run) if any of:
* `count_attempts(contact_id) >= MAX_ATTEMPTS` (=2)
* `normalize_phone(phone)` returns `None` (no usable number)
* `hubspot_contact_id` is missing (defensive — shouldn't happen for
  list members but the API surface allows it)

Skipped contacts are printed with a reason so the user can see why.

### 3. Interval scheduling
Default 5 s. Source of value in priority order:
1. `--interval N` CLI flag
2. `CALL_INTERVAL_SECONDS` env var (no edit to `.env.example` /
   `config.py` per the user's "don't modify existing files" directive
   — the var is purely opt-in)
3. fallback `5`

The interval is applied **between** placements, not before the first
one. After the last placement we don't sleep at all — `run_once`
returns immediately so cron / `--loop` can drive cadence.

### 4. `--dry-run`
Lists the contacts that *would* be called (with reasons for any
skips) but places no Twilio calls. Safe to run anytime.

### 5. `--loop`
For demo / dev sessions: re-runs `run_once` every `LOOP_INTERVAL`
seconds (default 60 s). Production should use cron and a single
`python main.py` per tick instead. Loop is opt-in; default invocation
is a single pass — matches `main.py` "scheduler bootstrap" semantics
in the README.

### 6. Twilio webhook is out of scope here
`scheduler.run_once` only *places* calls. The Flask app
(`call_handler.py`) must already be running and reachable on
`WEBHOOK_BASE_URL` — same contract as Phases 1-3.

### 7. Why business-hours windows are deferred
The README windows (10:00–11:30 AM, 2:00–3:30 PM local) are about
not annoying real practices during off-hours. With 2 test contacts
and the user dialling their own numbers for verification, the
windowing logic would just add latency and complexity to a test
session. The structure of `run_once` is:

```
contacts = list_contacts()
candidates = pending_contacts(contacts)
for c in candidates:
    place_call(...)
    sleep_between_placements()
```

When the real list arrives, `pending_contacts` gains a "is now inside
this contact's local window?" filter (using the contact's timezone
property), and `sleep_between_placements` becomes "sleep until next
window opens". Both are pure additions; no existing call sites need
to move.

## Out of scope (later phases / future)
- Time-zone-aware business-hours windows
- Per-contact local time zone field in HubSpot
- Dashboard (Phase 5)
- Personalized voicemails / live AI agent (Phase 6)

## Manual verification

Prerequisites:
* `.env` populated as for Phase 3 (Twilio + HubSpot + Webhook).
* Flask webhook running (`flask --app call_handler run --port 5000`)
  reachable via `WEBHOOK_BASE_URL` (ngrok in dev).

Steps:

```bash
# 1. Dry run first — confirm the 2 contacts are pending
python main.py --dry-run
# Expect: [scheduler] would call contact <id1> (Name) -> +1...
#         [scheduler] would call contact <id2> (Name) -> +1...
#         [scheduler] 2 contact(s) would be dialed, 0 skipped

# 2. Real run with 5-second spacing
python main.py
# Expect, ~5 seconds apart:
#   [scheduler] dialing contact <id1> ...
#   [twilio_client] placed call CallSid=... to=+1...
#   (5 second pause)
#   [scheduler] dialing contact <id2> ...
#   [twilio_client] placed call CallSid=... to=+1...
# Then in the Flask terminal: the Phase 2/3 [voice], [status],
#   [hubspot] lines for each call.

# 3. Re-run — both contacts now have 1 attempt; will be dialed again
python main.py
# Expect: 2 calls placed again (attempt 2 of 2).

# 4. Third run — both at MAX_ATTEMPTS; skip
python main.py
# Expect: [scheduler] skip contact <id1>: 2 attempts already made
#         [scheduler] skip contact <id2>: 2 attempts already made
#         [scheduler] 0 contact(s) dialed, 2 skipped
```
