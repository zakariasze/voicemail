# Workflow Overview — Live Pickup + Voicemail SMS

Implements the two "On Every Call" pieces from `workflow overview.pdf`
that were not yet in the codebase:

1. **Best Case — Voicemail picks up**
   *"An SMS is sent to the same number within seconds: a short
    follow-up message reinforcing the voicemail and asking them to
    call back."*

2. **Best Opportunity — A human picks up**
   *"The system plays a short hold message — and simultaneously rings
    your entire team at once. First to answer gets the call. If
    nobody answers within 20 seconds, a courteous message plays and
    the call ends cleanly."*

## Decisions

- **Transfer technology: Twilio `<Dial>` with multiple `<Number>`
  children.** Twilio rings every child number in parallel ("ringAll")
  and bridges the first to answer; the rest are released
  automatically. No call-queue product or third-party PBX is needed.
- **`answerOnBridge="true"`** keeps the practice's leg alive (and
  hearing ringback) until a closer actually picks up — this is what
  prevents the awkward "robocall" feel the PDF calls out.
- **Ring timeout: configurable, default 20 seconds** per the PDF.
  Stored in `TRANSFER_RING_TIMEOUT_SECONDS`.
- **`Transferred` outcome added** as a new value alongside the existing
  five. `Human Answered` is preserved for the "we reached a person but
  no closer picked up in time" case so the team can see missed
  transfers separately from successful ones.
- **Hold + goodbye recordings are optional.** If `HOLD_RECORDING_URL`
  or `GOODBYE_RECORDING_URL` are unset, the code falls back to a
  `<Say>` so the system is still functional pre-rollout.
- **`CLOSER_NUMBERS` empty ⇒ silent hangup on human pickup.** Same
  behavior as before this change, so deploying the new code with no
  closers configured is safe.
- **SMS follow-up is fired in a background `threading.Thread`** from
  `/voice` so the TwiML response (which starts the recording playback)
  is not blocked on a Twilio REST round-trip. The thread re-checks
  `sms_sent_at` so Twilio `/voice` retries don't double-send.
- **SMS send failures are swallowed and logged.** The voicemail is the
  product; the SMS is a nice-to-have follow-up. It must never break
  the call flow. Same philosophy as the existing HubSpot logging in
  `/status`.
- **SMS body is env-configurable** (`SMS_FOLLOWUP_BODY`) with a short
  generic default. No HubSpot property write yet — kept out of scope
  for this change, but `sms_sent_at` is recorded in SQLite so a later
  reconcile step can backfill HubSpot if desired.

## New / changed surface

| File | Change |
|------|--------|
| `state.py` | `OUTCOME_TRANSFERRED`; new `sms_sent_at` column + `mark_sms_sent`. |
| `config.py` | `closer_numbers()`, `hold_recording_url()`, `goodbye_recording_url()`, `transfer_ring_timeout_seconds()`, `sms_followup_body()`. |
| `twilio_client.py` | `send_sms(to_number, body)` helper. |
| `call_handler.py` | `/voice` plays hold + dials closers on human pickup; fires SMS in a background thread on voicemail. New `/transfer-status` endpoint finalizes the live transfer. |
| `hubspot_client.py` | `Transferred` added to the `last_call_outcome` enum options. |
| `dashboard.py` | `Transferred` pill (server template + JS port + CSS). |
| `.env.example` | New `CLOSER_NUMBERS`, `HOLD_RECORDING_URL`, `GOODBYE_RECORDING_URL`, `TRANSFER_RING_TIMEOUT_SECONDS`, `SMS_FOLLOWUP_BODY`. |
| `README.md` | "How It Works" updated for both new pieces. |

## Verification

1. **Voicemail + SMS** — Call a number with a voicemail box. Confirm:
   - Recording plays after the beep.
   - `[twilio_client] sent SMS sid=…` appears in the Flask log.
   - SQLite row has `outcome='Voicemail Left'` and `sms_sent_at` set.

2. **Live transfer (closer answers)** — Call a number you answer
   yourself with `CLOSER_NUMBERS` set to a closer line you can reach.
   Confirm:
   - Practice hears the hold message, then ringback, then is bridged
     to the closer.
   - SQLite row ends at `outcome='Transferred'`.

3. **Live transfer (nobody answers)** — Same as above, but don't
   answer any closer line. Confirm:
   - Practice hears the goodbye message and the call hangs up cleanly
     after `TRANSFER_RING_TIMEOUT_SECONDS`.
   - SQLite row stays at `outcome='Human Answered'`.
