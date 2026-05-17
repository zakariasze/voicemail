# Phase 1 — Foundation

> Scope per `README.md → Build Phases → Phase 1`:
> *"Get a single call placed and logged. Nothing else."*

This document describes what Phase 1 builds, the decisions made and why,
and how to verify it manually. **No Phase 2+ code is included.**

## What is in Phase 1

| File | Purpose |
|---|---|
| `config.py` | Loads env vars from `.env` (via `python-dotenv`). Centralised so other modules never read `os.environ` directly. Fails fast with a clear error if required vars are missing when `require()` is called. |
| `twilio_client.py` | One function: `place_call(to_number)`. Places one outbound call via Twilio with AMD enabled (`machine_detection='DetectMessageEnd'`) and points Twilio's async AMD callback at our Flask webhook. Also exposes a tiny CLI entry point (`python twilio_client.py +15551234567`) for the verification step. |
| `call_handler.py` | Minimal Flask app. Two endpoints: `/voice` returns a trivial `<Hangup/>` TwiML so the call doesn't loop, and `/amd` prints the AMD result (`CallSid`, `AnsweredBy`, etc.) to stdout. Phase 2 will replace the trivial TwiML with the actual voicemail playback logic — for now, **we only need to see the AMD result print**. |
| `.env.example` | Mirrors the env vars listed in the README, plus optional `WEBHOOK_PATH_*` overrides. |
| `requirements.txt` | `flask`, `twilio`, `python-dotenv`. Three deps total. Versions pinned to known-stable majors. |
| `.gitignore` | Keeps `.env`, `recordings/`, `*.db`, `__pycache__/` out of git. |

## Decisions made and why

### 1. AMD mode: `DetectMessageEnd` (not `Enable`)
Phase 2 will play a pre-recorded MP3 only after the beep. If we used
`Enable`, AMD would fire as soon as it decides machine-vs-human, often
mid-greeting, and our recording would start over the prompt. The Twilio
docs explicitly recommend `DetectMessageEnd` for voicemail-drop use
cases. Slight cost trade-off (a few extra seconds of paid call time per
voicemail), but accuracy gain is decisive.

### 2. Async AMD callback (not blocking TwiML branching)
We pass `async_amd_status_callback` to Twilio. AMD runs in the
background and POSTs `AnsweredBy` to `/amd` when it completes. Phase 1
only needs to print this value; Phase 2 will use it to branch outcomes.

For Phase 1 the primary call URL (`/voice`) returns `<Hangup/>` so the
test call disconnects cleanly. This is correct for Phase 1: we are not
yet trying to play a voicemail, we are just confirming end-to-end
plumbing (Twilio → our webhook → AMD result printed).

### 3. Flask over FastAPI
README suggests Flask; Twilio's Python examples use Flask; we don't need
async I/O at this layer (one webhook hit per call, throughput is tiny).
One fewer concept on the maintenance surface.

### 4. Pinned deps, no extras
`twilio`, `flask`, `python-dotenv` only. No requests-toolbelt, no
gunicorn, no FastAPI. We will add SQLite (stdlib) in Phase 2, HubSpot
SDK in Phase 3, APScheduler in Phase 4. The design constraint is
"minimise API/lib surface".

### 5. `WEBHOOK_BASE_URL` is required at call time, not import time
During local development this will be an ngrok / cloudflared URL that
changes. `config.require('WEBHOOK_BASE_URL')` only runs when we actually
place a call, so the Flask app can boot even before the tunnel is up.

### 6. No tests yet
Phase 1's deliverable per the README is a **manual verification step**,
not an automated test suite. We add a tiny CLI in `twilio_client.py` for
that single manual test. Automated tests will start in Phase 2 around
`state.py` where they pay off.

## Manual verification (the Phase 1 acceptance test)

```bash
# 0. one-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER in .env

# 1. expose Flask to the internet
flask --app call_handler run --port 5000
# in another terminal:
ngrok http 5000
# copy the https URL and set it in .env as WEBHOOK_BASE_URL

# 2. place one real test call
python twilio_client.py +15551234567   # your own mobile, let it ring to voicemail

# 3. observe
# In the Flask terminal you should see a line like:
#   [AMD] CallSid=CAxxxx... AnsweredBy=machine_end_beep
# (or AnsweredBy=human if you pick up)
```

That is the entire Phase 1 acceptance criterion: **one real call placed,
one AMD result printed**. No persistence, no HubSpot, no scheduling.

## Out of scope for Phase 1 (will live in later phases)

- TwiML that plays `voicemail.mp3` on machine, silent hang-up on human → Phase 2.
- SQLite outcome log → Phase 2.
- HubSpot contact pull / activity write-back / custom properties → Phase 3.
- Timezone-aware scheduler and 2-attempt tracking → Phase 4.
- Dashboard → Phase 5.

**Do not build any of these until the user confirms Phase 1.**
