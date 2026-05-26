# Voicemail-Send — Project Overview

A Python/Flask + Twilio + HubSpot agent that places automated outbound calls,
drops a pre-recorded voicemail when it detects an answering machine, logs the
outcome to HubSpot, and (with the new additions) sends a follow-up SMS plus
forwards live answers to a priority team.

## High-level flow

```
HubSpot list  ──►  scheduler.py  ──►  twilio_client.place_call()  ──►  Twilio
                                                                       │
                                                                       ▼
                                                            AMD (Answering
                                                            Machine Detection)
                                                                       │
                ┌──────────────────────────────────────────────────────┤
                ▼                                                      ▼
        Machine detected                                       Human answered
        POST /voice  ──► <Play>voicemail.mp3</Play>            POST /voice  ──► forward flow
                ──► record "Voicemail Left"                    (new: play hold + dial team)
                ──► (new) send follow-up SMS
                                                                       │
                                                                       ▼
                                                             POST /status (terminal)
                                                                       │
                                                                       ▼
                                                              HubSpot call log
                                                              + custom properties
```

## Components

### `main.py`
Bootstrap entry point. Ensures the SQLite schema exists, ensures the two
HubSpot custom contact properties exist (`last_call_attempt`,
`last_call_outcome`), and hands off to `scheduler._main()`. CLI flags pass
through to the scheduler (`--dry-run`, `--interval N`, `--loop`).

### `config.py`
Single source of truth for environment variables. Loads `.env` once at
import. Exposes typed accessors (`twilio_account_sid()`,
`webhook_base_url()`, `hubspot_list_id()`, …). `require(name)` raises
`RuntimeError` at the boundary if a required variable is missing.

### `state.py`
SQLite-backed persistence. Stdlib-only. Two tables:

- **`calls`** — one row per Twilio `CallSid`. Columns: `call_sid`,
  `to_number`, `outcome`, `answered_by`, `call_status`,
  `hubspot_contact_id`, `hubspot_logged_at`, `created_at`, `updated_at`.
  Additive `ALTER TABLE` migrations run automatically at `init_db()`.
- **`campaigns`** — `(id, name, hubspot_list_id, status)` where status is
  `active | paused | done`.

Key helpers: `record_call_placed`, `record_outcome` (won't overwrite a
non-NULL outcome with NULL), `mark_hubspot_logged`, `get`,
`list_recent`, `phone_call_stats`, plus campaign CRUD.

Outcome constants: `Voicemail Left`, `Human Answered`, `No Answer`,
`Busy`, `Failed`.

### `twilio_client.py`
`place_call(to_number, hubspot_contact_id=…)` — places one outbound call
with synchronous AMD configured as `DetectMessageEnd`. Twilio waits for
the voicemail-greeting beep before calling `/voice`, so the recording
plays cleanly after the beep. Registers `/status` as the
`statusCallback` for the `completed` event, then writes a placement
row to SQLite so the webhooks can look the contact up by `CallSid`.

### `call_handler.py` (Flask webhook, port 5000)
- `POST /voice` — Twilio's TwiML webhook. Reads `AnsweredBy` and branches:
  - `machine_*` → `<Play>{VOICEMAIL_RECORDING_URL}</Play><Hangup/>`,
    outcome = "Voicemail Left".
  - `human` → silent `<Pause length="1"/><Hangup/>`, outcome = "Human
    Answered". *(This is the branch the new call-forwarding feature
    replaces.)*
  - `fax` / `unknown` → silent hang up, outcome = "Failed" or
    "No Answer".
- `POST /status` — Twilio's terminal `statusCallback`. Records final
  `CallStatus` for calls that never reached `/voice` (no-answer, busy,
  failed, canceled). Calls `_maybe_log_to_hubspot()` to push the outcome
  to HubSpot exactly once.
- `GET /healthz` — liveness probe.

### `hubspot_client.py`
Thin v3 REST client (no SDK, just `requests`). Surface:
- `ensure_custom_properties()` — idempotently creates
  `last_call_attempt` (datetime) and `last_call_outcome` (enumeration).
- `list_contacts(list_id=…)` — paginates the list-membership endpoint
  then batch-reads contacts (firstname, lastname, phone).
- `get_contact(id)`.
- `log_call(contact_id, outcome=…, duration_seconds=…)` — writes a Call
  engagement to the contact timeline and PATCHes the two custom
  properties.
- `normalize_phone(raw)` — strips junk, prepends `+1` for 10-digit US
  numbers, returns E.164.

### `scheduler.py`
The dialing loop.
- `MAX_ATTEMPTS = 2` — README cap.
- `count_attempts(contact_id)` — counts placements in the `calls`
  table.
- `pending_contacts(contacts)` — splits a HubSpot list into
  `(to_call, skipped_with_reason)`, filtering on usable phone and
  attempts < `MAX_ATTEMPTS`.
- `_pick_source()` — picks the active campaign's HubSpot list if one
  exists, else falls back to `HUBSPOT_LIST_ID`.
- `run_once(interval_seconds=None, dry_run=False)` — one pass.
- CLI: `python scheduler.py [--dry-run] [--interval N] [--loop]
  [--loop-interval N]`.

### `dashboard.py` (Flask UI, port 5001)
Campaign management UI. Lists campaigns, creates them
`(name, hubspot_list_id)`, toggles `Start / Pause / Done`. Detail page
polls `/campaigns/<id>/contacts.json` for live updates: spinner while
`in_progress=True`, then attempt count / last outcome / last call time.
No auth — same posture as the webhook.

## Storage

- `voicemail.db` — SQLite, single file. Path override:
  `VOICEMAIL_DB_PATH`.

## Required environment variables

(Values intentionally omitted — see `.env.example` for the full list.)

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- `VOICEMAIL_RECORDING_URL` — public HTTPS URL of the voicemail .mp3
- `WEBHOOK_BASE_URL` — public HTTPS base where Twilio reaches the
  Flask webhook (e.g. ngrok URL, no trailing slash)
- `HUBSPOT_API_KEY`, `HUBSPOT_LIST_ID`
- `DASHBOARD_PORT` (optional)

## Running locally

```bash
# Terminal A: webhook
flask --app call_handler run --port 5000

# Terminal B: scheduler (one pass)
python main.py
# or loop
python main.py --loop --interval 5

# Terminal C: dashboard
flask --app dashboard run --port 5001
```

## Outcome → action matrix

| AnsweredBy / status      | Current behavior              | After upgrade                                    |
|--------------------------|-------------------------------|--------------------------------------------------|
| `machine_*`              | Play voicemail, hang up       | Play voicemail, hang up, **send SMS**            |
| `human`                  | Silent hang up                | **Play "please hold" MP3, dial 3 priority numbers in order, bridge call** |
| `fax`                    | Silent hang up (Failed)       | unchanged                                        |
| `unknown`                | Silent hang up (No Answer)    | unchanged                                        |
| `no-answer / busy / failed / canceled` | Recorded by `/status` | unchanged                                        |

---

# Planned additions

## 1. SMS follow-up after voicemail

**Goal:** the moment a voicemail drop completes successfully, fire a
single SMS to the same number using a configurable script.

**Trigger point:** `call_handler.voice()` — after
`state.record_outcome(... outcome=OUTCOME_VOICEMAIL_LEFT ...)` and
*before* returning the TwiML. Sending from `/voice` rather than
`/status` keeps the SMS tightly coupled to the actual voicemail-left
event (no risk of firing on no-answer or human).

**New config keys** (added to `.env.example`, accessed via `config.py`):
- `SMS_FOLLOWUP_ENABLED` — `"true"` / `"false"`. Default off.
- `SMS_FOLLOWUP_BODY` — the message template. Supports `{first_name}`
  and `{last_name}` placeholders, filled from the HubSpot contact row.
  Default (used if unset):
  > Hi {first_name}, this is Kai — just left you a voicemail. Give me a
  > call back when you have a minute. Thanks!

**New code:**
- `twilio_client.send_sms(to_number, body)` — wraps
  `client.messages.create(to=…, from_=TWILIO_FROM_NUMBER, body=…)`.
- `call_handler._send_followup_sms(call_sid, to_number)` — best-effort:
  reads the contact name from HubSpot (or skips placeholders if no
  contact id), renders the template, calls `send_sms`. Errors are
  logged but do not affect the TwiML returned to Twilio.

**Duplicate protection:** a new SQLite column `sms_sent_at` on the
`calls` table. `_send_followup_sms` is a no-op if it's already set,
so Twilio retries of `/voice` cannot double-send.

**Migration:** added to `state._MIGRATIONS` so existing DBs upgrade.

## 2. Call forwarding on human answer

**Goal:** when a real person picks up, don't hang up — keep them on the
line with a "please hold" recording while we ring three priority
numbers in order and bridge the call to whoever answers first.

**Trigger point:** `call_handler.voice()`, in the branch where
`AnsweredBy == "human"`. Today that branch returns silent hangup; we
replace it with a `<Play>` + `<Dial>` TwiML sequence.

**TwiML returned to the caller (the live human):**
```xml
<Response>
  <Play>{HOLD_RECORDING_URL}</Play>
  <Dial action="/forward-status" timeout="20" answerOnBridge="true">
    <Number url="/forward-whisper">{PRIORITY_NUMBER_1}</Number>
    <Number url="/forward-whisper">{PRIORITY_NUMBER_2}</Number>
    <Number url="/forward-whisper">{PRIORITY_NUMBER_3}</Number>
  </Dial>
</Response>
```

Twilio dials all three `<Number>` legs in parallel; the first to pick
up is bridged, the rest are hung up. With `answerOnBridge="true"` the
inbound contact continues to hear the hold music until a priority
number actually answers.

**New config keys:**
- `CALL_FORWARDING_ENABLED` — `"true"` / `"false"`. Default off so
  existing deployments don't surprise users.
- `HOLD_RECORDING_URL` — public HTTPS URL of the "please hold" MP3.
- `PRIORITY_NUMBER_1`, `PRIORITY_NUMBER_2`, `PRIORITY_NUMBER_3` — E.164
  numbers, dialed in that order of preference.

**New endpoints (in `call_handler.py`):**
- `POST /forward-whisper` — returns a short `<Say>` so the answering
  team member hears "Incoming forwarded call" before the bridge
  completes (lets them differentiate from a normal call).
- `POST /forward-status` — Twilio's `<Dial action=…>` callback. Reads
  `DialCallStatus` (`completed`, `no-answer`, `busy`, `failed`). If
  all three priority lines fail, returns `<Hangup/>`; on success,
  records outcome = "Human Answered" with a `forwarded_to` annotation.

**State additions:**
- New column `forwarded_to` on `calls` (TEXT, nullable) — the
  E.164 of the priority leg that answered, or NULL if none did.
- New column `forward_status` on `calls` — the raw
  `DialCallStatus`. Both added via `_MIGRATIONS`.

**Failure mode:** if `CALL_FORWARDING_ENABLED` is true but
`HOLD_RECORDING_URL` or any of the three numbers is missing, the
endpoint logs a warning and falls back to the current silent-hangup
behavior rather than crashing the call.

## Open questions / decisions to confirm

- Should SMS only fire on the *first* successful voicemail drop per
  contact, or on every attempt? (Default in the plan above: every
  attempt, since `MAX_ATTEMPTS=2` already caps it.)
- Should the dashboard surface SMS-sent / forwarded-to columns? Easy
  to add once the SQLite columns exist.
- Priority list — fixed in env, or per-campaign? Env is simpler for
  now; per-campaign would mean a new column on `campaigns`.
