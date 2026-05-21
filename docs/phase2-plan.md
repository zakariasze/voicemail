# Phase 2 — Voicemail playback + outcome logging

> Scope per `README.md → Build Phases → Phase 2`:
> *"TwiML that plays voicemail.mp3 when AMD detects a machine; TwiML that
> hangs up silently when AMD detects a human; SQLite table to record each
> call's outcome and timestamp."*

This document covers what Phase 2 builds, the decisions made and why,
and how to verify it manually. **No Phase 3+ code is included.**

## What changed since Phase 1

In Phase 1, `/voice` returned a trivial `<Hangup/>` so the call would
disconnect cleanly. That's why the user heard *"an application error has
occurred"* — Twilio plays that prompt when the very first TwiML verb on
a call is `<Hangup/>` with no prior audio. Phase 2 replaces it with
real branching TwiML.

## What is in Phase 2

| File | Change |
|---|---|
| `twilio_client.py` | Switch from **async AMD** to **synchronous AMD** (drop `async_amd*` params, keep `machine_detection='DetectMessageEnd'`). Twilio now holds the TwiML request until AMD completes and passes `AnsweredBy` as a form parameter to `/voice`. Also adds a `status_callback` to capture terminal call states (`no-answer`, `busy`, `failed`, `completed`) which never fire `/voice`. |
| `call_handler.py` | `/voice` now reads `AnsweredBy` and returns branching TwiML: `<Play>{recording}</Play><Hangup/>` for machine outcomes, silent `<Hangup/>` for human/unknown. Each branch records the outcome to SQLite. New `/status` endpoint records terminal call states for calls that never connect. The Phase 1 `/amd` async endpoint is **removed** — sync AMD is simpler and matches our flow. |
| `state.py` | New module. SQLite-backed table `calls` with one row per `CallSid`. Upsert API: `record_outcome(call_sid, to_number, outcome, answered_by=None, call_status=None)`. Tiny query helpers used by the manual verification step (Phase 5's dashboard will use the same module). |
| `recordings/.gitkeep` | Placeholder so the directory exists; the actual `voicemail.mp3` is gitignored per the README. |
| `.env.example` | `VOICEMAIL_RECORDING_URL` clarified as **required for Phase 2 onward**. |

## Decisions and why

### 1. Synchronous AMD over async AMD
With `async_amd=True` (Phase 1), `/voice` fires immediately when the
call connects, *before* AMD has decided machine-vs-human. Branching on
the outcome in a single TwiML response is impossible — you'd have to
`<Pause>` and `<Redirect>` after the result, which is fragile and
adds a second webhook round-trip per call.

With synchronous AMD (Phase 2), Twilio holds the TwiML request until
AMD completes (typically 1–4 seconds for `DetectMessageEnd`) and then
includes `AnsweredBy` in the POST body. A single `/voice` response can
branch correctly. This is the pattern in every well-known Twilio AMD
example. Slight latency trade-off (call connects but no audio plays
until AMD returns) is acceptable because we always want to wait for the
beep on machines anyway.

### 2. Map Twilio's `AnsweredBy` values to README outcomes

| Twilio `AnsweredBy` | README outcome | TwiML |
|---|---|---|
| `machine_end_beep` | `Voicemail Left` | `<Play>{url}</Play><Hangup/>` |
| `machine_end_silence` | `Voicemail Left` | `<Play>{url}</Play><Hangup/>` |
| `machine_end_other` | `Voicemail Left` | `<Play>{url}</Play><Hangup/>` |
| `machine_start` | `Voicemail Left` | `<Play>{url}</Play><Hangup/>` *(rare with DetectMessageEnd — included defensively)* |
| `human` | `Human Answered` | `<Hangup/>` (silent) |
| `fax` | `Failed` | `<Hangup/>` |
| `unknown` | `Human Answered` | `<Hangup/>` *(conservative: assume a human picked up; better to skip the drop than risk playing a recording at a person)* |

This mapping is **conservative on the side of not playing a recording
at a real human**. That is the correct default for cold outreach: a
missed voicemail drop is annoying; an unsolicited recording played at a
human is a complaint and a legal risk.

### 3. `status_callback` for calls that never reach `/voice`
Twilio only invokes `/voice` if the call actually connects. For
`no-answer`, `busy`, and `failed`, `/voice` is never called. We need
those outcomes too, so we register a `status_callback` (events:
`completed`) that fires once at the end of every call regardless of
outcome. `/status` records:

| Twilio `CallStatus` | README outcome |
|---|---|
| `no-answer` | `No Answer` |
| `busy` | `Busy` |
| `failed` / `canceled` | `Failed` |
| `completed` | already recorded by `/voice` — `/status` does an upsert and only fills in `call_status` if no outcome is set yet |

To avoid race conditions, the outcome is recorded by **whichever
endpoint runs first**. A `completed` status callback will not overwrite
an already-set outcome (e.g. `Voicemail Left`) — it only writes
`call_status` and a missing-outcome fallback.

### 4. SQLite, single file, stdlib only
README §"What the Agent Should Research and Decide" §6 mandates a
local SQLite state table as the source of truth. Python's stdlib
`sqlite3` covers it; no ORM. One table, eight columns, plain SQL.
File path defaults to `./voicemail.db` (gitignored).

### 5. Upsert semantics
`record_outcome` is idempotent: a row is keyed by `CallSid` and updates
in place. Replaying a webhook (Twilio retries on 5xx) won't double-log.
The `updated_at` column moves on every write; `created_at` is set once.

### 6. No automated tests yet
Phase 2's deliverable is still a manual verification step. We do add
a tiny in-memory smoke test path in `state.py` (`__main__`) that
verifies the schema round-trips correctly, callable as
`python state.py` — this is enough sanity-checking for now.
Phase 3 will introduce real pytest tests when we start interacting
with HubSpot.

## Manual verification (Phase 2 acceptance)

```bash
# 0. ensure Phase 1 setup is still in place (.env populated, tunnel up)
flask --app call_handler run --port 5000
# Make sure VOICEMAIL_RECORDING_URL points at a publicly-fetchable
# .mp3 (Twilio Assets is easiest).

# 1. call your own number, let it ring to voicemail
python twilio_client.py +1YOUR_MOBILE
# Expect:
#   - Your phone rings, then voicemail picks up
#   - After the beep, the recording plays
#   - Flask terminal shows:
#       [voice] CallSid=CAxxx AnsweredBy=machine_end_beep -> Voicemail Left
#       [status] CallSid=CAxxx CallStatus=completed

# 2. call your own number and answer it as a human
python twilio_client.py +1YOUR_MOBILE
# Expect:
#   - You hear silence for ~1s then the call ends
#   - Flask terminal shows:
#       [voice] CallSid=CAxxx AnsweredBy=human -> Human Answered
#       [status] CallSid=CAxxx CallStatus=completed

# 3. confirm SQLite has both outcomes
sqlite3 voicemail.db "SELECT call_sid, to_number, outcome, answered_by, call_status FROM calls;"
# Two rows, one per call, with the expected outcomes.
```

## Out of scope for Phase 2 (will live in later phases)

- HubSpot contact pull / activity write-back / custom properties → Phase 3.
- Timezone-aware scheduler and 2-attempt tracking → Phase 4.
- Dashboard → Phase 5.

**Do not build any of these until the user confirms Phase 2.**
