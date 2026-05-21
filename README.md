# Voicemail Automation Agent

## Purpose

Automate cold-call outreach to dental practices. The system reads a contact list from HubSpot each morning, calls each practice during their local business hours, drops a pre-recorded voicemail when the machine picks up, hangs up silently if a human answers, and writes every outcome back to HubSpot — no human dialing required.

> **See `process overview.pdf`** in this repo for a visual flowchart of the full system, outcome decision tree, and dashboard mockup. Refer to it whenever the written description is ambiguous.

---

## Agent Instructions

> **Before writing a single line of code, you must search the internet.** This is not optional. Look for:
> - Existing open-source projects that do outbound voicemail dropping (GitHub, HuggingFace, Reddit, Hacker News, ProductHunt)
> - Twilio AMD + voicemail drop implementations (search "twilio answering machine detection voicemail drop python github")
> - HubSpot + Twilio call logging integrations (search "hubspot twilio outbound call logging python")
> - Any SaaS tools that already solve this (e.g. SlyDial, DropCowboy, VoiceDrop) — understand how they work even if we're not using them
> - Blog posts, tutorials, or Stack Overflow threads about automated voicemail campaigns
>
> **Summarize what you found** before starting Phase 1: what exists, what you can reuse or learn from, and what gaps this project fills. If a well-maintained open-source repo already solves 80% of this, say so and propose building on top of it instead of from scratch.

> **To the agent reading this:** Work in strict phases. **Do not build the next phase until explicitly told to.** Each phase must be fully working, tested, and confirmed before any new code is added. The goal is a system that is always in a runnable state — never a half-built mess.
>
> For each phase:
> 1. Start by researching and writing a short plan (what you will build, what decisions you made and why).
> 2. Build only what is listed for that phase.
> 3. Write a simple smoke-test or manual verification step so the user can confirm it works.
> 4. Stop and wait for confirmation before proceeding to the next phase.
>
> When decisions are open, pick the option that best satisfies the design constraints. Document every decision you make and why.

---

## How It Works (Plain English)

1. **6:00 AM** — System wakes, pulls today's call list from HubSpot (a curated list of dental practices).
2. **6:05 AM** — Schedules each contact for two call attempts in their own time zone: 10:00–11:30 AM local, 2:00–3:30 PM local.
3. **10:00 AM onward** — Calls go out automatically via Twilio. No one needs to be at a desk.
4. **Each call** — Answering Machine Detection (AMD) determines who/what picked up:
   - **Voicemail detected** → play the pre-recorded `.mp3` message in full, then hang up. Log as `Voicemail Left`. A follow-up **SMS is sent automatically** to the same number reinforcing the voicemail and asking for a callback.
   - **No answer / rang out** → Log as `No Answer`.
   - **Human detected** → play a short *"please hold…"* message and simultaneously ring every number in `CLOSER_NUMBERS`. First closer to answer is bridged in; the rest are released. If nobody picks up within `TRANSFER_RING_TIMEOUT_SECONDS` (default 20s) a courteous goodbye plays and the call ends cleanly. Log as `Transferred` when a closer answered, otherwise `Human Answered`.
   - **Failed / busy / bad number** → Log as `Failed` or `Busy`.
5. **After each call** — A call activity is added to the contact's HubSpot timeline. Two contact properties are updated: `Last Call Attempt` (datetime) and `Last Call Outcome` (enum).
6. **End of day** — A simple dashboard page shows: total dialed, outcome breakdown, and which contacts need human follow-up.

---

## Design Constraints

| Constraint | Requirement |
|---|---|
| AI/ML | Prefer open-source models over proprietary APIs |
| Cost | Minimize paid API surface — only pay for what is genuinely irreplaceable (e.g. Twilio call minutes) |
| API count | Keep the number of third-party integrations as small as possible |
| Complexity | Simple, maintainable Python — no over-engineering |
| Deployment | Should run on a single cheap VPS or local machine via cron |

---

## Required Integrations (non-negotiable)

- **HubSpot** — source of contacts and destination for call logs. Use the HubSpot v3 API (contacts, engagements/calls, custom properties).
- **Twilio** — the only viable low-cost programmable telephony option for placing outbound calls with AMD. Use Twilio's built-in AMD (`MachineDetection=DetectMessageEnd`) to avoid needing a separate ML model for detection.

---

## What the Agent Should Research and Decide

1. **Best open-source TTS or voice synthesis** (if personalized voicemails are wanted in future) — research current best options (e.g. Coqui TTS, Piper, Kokoro) but Phase 1 uses a single static human-recorded `.mp3`, so this is Phase 2 research only.
2. **AMD accuracy trade-offs** — Twilio built-in AMD vs. self-hosted classifier. Recommend the lowest-cost approach that gets >90% accuracy. Document the trade-off.
3. **Scheduler** — recommend between APScheduler (in-process), Celery + Redis, or plain cron + a queue table in SQLite. Prefer the simplest option that handles timezone-aware scheduling reliably.
4. **Dashboard** — a minimal read-only status page (Flask or FastAPI + plain HTML, no frontend framework needed). Should show: date, total called, outcome counts, list of contacts with outcomes.
5. **HubSpot custom properties** — determine whether `Last Call Attempt` and `Last Call Outcome` need to be created via API on first run or manually in HubSpot. Provide the API call to create them programmatically.
6. **Retry logic** — how to track which contacts have had attempt 1 vs. attempt 2, across restarts. Recommend a local SQLite state table as the source of truth (HubSpot is the log, not the scheduler state).

---

## Suggested Project Structure

```
voicemail/
├── .env.example            # all required environment variables
├── requirements.txt
├── README.md
├── main.py                 # entry point / scheduler bootstrap
├── config.py               # loads env vars, constants (time windows, timezone defaults)
├── hubspot_client.py       # pull contacts, log call activities, update properties
├── twilio_client.py        # place outbound call, handle TwiML webhook response
├── scheduler.py            # timezone-aware call scheduling logic
├── call_handler.py         # AMD webhook endpoint (Flask/FastAPI), routes outcomes
├── state.py                # SQLite-backed call state (attempt tracking, dedup)
├── dashboard.py            # read-only status page
├── twiml/
│   └── voicemail.xml       # TwiML to play recording and hang up
└── recordings/
    └── voicemail.mp3       # the human-recorded message (not committed to git)
```

---

## Environment Variables Needed

```
HUBSPOT_API_KEY=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
VOICEMAIL_RECORDING_URL=    # publicly accessible URL to the .mp3 (can be Twilio-hosted)
WEBHOOK_BASE_URL=           # public URL this server is reachable at (for Twilio callbacks)
DASHBOARD_PORT=5000
```

---

## Build Phases

> **Rule: build one phase at a time. Stop after each phase and wait for confirmation.**

### Phase 1 — Foundation (build first)
Get a single call placed and logged. Nothing else.

- [ ] `config.py` — load env vars
- [ ] `twilio_client.py` — place one outbound call with AMD enabled
- [ ] `call_handler.py` — minimal Flask webhook that receives the AMD result and prints it
- [ ] `.env.example` + `requirements.txt`
- [ ] **Verification:** place a test call to a real number, confirm AMD result prints in the terminal

### Phase 2 — Voicemail playback + outcome logging
Only after Phase 1 is confirmed working.

- [ ] TwiML response that plays `voicemail.mp3` when AMD detects a machine
- [ ] TwiML response that hangs up silently when AMD detects a human
- [ ] `state.py` — SQLite table to record each call's outcome and timestamp
- [ ] **Verification:** call a number with voicemail, confirm the recording plays and outcome is saved to SQLite

### Phase 3 — HubSpot integration
Only after Phase 2 is confirmed working.

- [ ] `hubspot_client.py` — pull contact list, log call activity, update `Last Call Attempt` and `Last Call Outcome` properties
- [ ] Script to create the two custom HubSpot properties on first run
- [ ] Wire outcome from Phase 2 into HubSpot logging
- [ ] **Verification:** run against one real HubSpot contact, confirm activity appears on their timeline

### Phase 4 — Scheduler
Only after Phase 3 is confirmed working.

- [ ] `scheduler.py` — timezone-aware scheduling, two windows per contact (10:00–11:30 AM, 2:00–3:30 PM local)
- [ ] Two-attempt tracking in SQLite (don't re-call if both attempts done)
- [ ] Entry point `main.py` that bootstraps the scheduler
- [ ] **Verification:** run scheduler with a small test list, confirm calls go out at the right local times

### Phase 5 — Dashboard
Only after Phase 4 is confirmed working.

- [ ] `dashboard.py` — minimal Flask page: date, total dialed, outcome counts, contact list with outcomes, human-follow-up flag
- [ ] **Verification:** run dashboard, confirm it reflects the SQLite state correctly

### Phase 6 (future — do not build yet)
- Personalized voicemails using open-source TTS (Piper / Kokoro)
- AI agent that holds a live conversation when a human picks up
- Smart callback routing
- Practice prioritization / scoring layer
