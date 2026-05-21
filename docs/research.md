# Internet Research Summary

> Mandatory pre-Phase-1 research step from `README.md → Agent Instructions`.
> Performed before any code was written.

## Question

Does an existing open-source project already solve >=80% of the problem
described in `README.md` (HubSpot-sourced outbound cold calls to dental
practices, Twilio AMD, drop a pre-recorded voicemail on machine, hang up
silently on humans, log everything back to HubSpot, timezone-aware
scheduler, minimal dashboard)?

**Short answer: No.** The combination of *Twilio AMD + voicemail drop +
HubSpot logging + timezone-aware two-attempt scheduler* does not exist as a
single maintained open-source project. Building from scratch (small,
modular Python) is justified. We can borrow patterns from the projects
below.

## What I searched

- GitHub repository search: `twilio voicemail drop python`,
  `twilio answering machine detection python`,
  `hubspot twilio call logging python`, `voicemail drop`,
  `outbound dialer twilio python`.
- GitHub code search: `MachineDetection DetectMessageEnd language:Python`
  (1k+ hits — pattern is well-documented; many small scripts use it).
- Twilio docs: Answering Machine Detection (`MachineDetection`,
  `AsyncAmdStatusCallback`, `AnsweredBy` values).
- SaaS landscape: SlyBroadcast, DropCowboy, SlyDial, VoiceDrop.

> Note: General web search engines (Google/Bing/DuckDuckGo) were blocked
> in the sandbox. GitHub search and direct repo fetches worked, which
> covered the open-source half of the research question. The Twilio docs
> URL was not directly reachable, but the AMD parameters are well-known
> from the Twilio Python helper library and dozens of code hits found via
> GitHub search.

## Most relevant prior art

| Repo | Stack | Verdict |
|---|---|---|
| [`phatjmo/vmdrop`](https://github.com/phatjmo/vmdrop) | Python, Twilio | Tiny script (2017, ~1★). Uses Twilio AMD to drop voicemail. Useful **pattern reference** only — no HubSpot, no scheduler, no state, no tests. |
| [`Cesars-dev/Voicemail_drops`](https://github.com/Cesars-dev/Voicemail_drops) | Python 3.12, async | HVAC vertical, but uses **SlyBroadcast (ringless VM) + ElevenLabs TTS + GPT-4o-mini**, not Twilio AMD. Different compliance profile and far more API surface than our design constraints allow. Useful as a structural reference for an async pipeline. |
| [`nexmo-community/python-voicemail-dead-drop`](https://github.com/nexmo-community/python-voicemail-dead-drop) | Python/Flask, Vonage | *Inbound* voicemail box, not outbound drop. Not applicable. |
| [`rixwankhan/Voicemail-Drop`](https://github.com/rixwankhan/Voicemail-Drop) | Unknown / sparse | Stale, no code of substance. |
| [`Updog8675309/Voicemail-Drop`](https://github.com/Updog8675309/Voicemail-Drop), [`abehara2/voicemail-dropper`](https://github.com/abehara2/voicemail-dropper) | Misc | Empty/toy. |
| [`SeekTom/Twilio-Client-Python-dialer`](https://github.com/SeekTom/Twilio-Client-Python-dialer), [`Sambit-7/AutoDialer-App`](https://github.com/Sambit-7/AutoDialer-App) | Python/Flask + Twilio | Outbound dialers, no AMD, no voicemail logic, no CRM. |

No HubSpot+Twilio integration repo exists with non-trivial activity.

## SaaS competitors (for context only — we are not using them)

- **SlyBroadcast / DropCowboy / VoiceDrop** — "ringless voicemail":
  deposit a recording directly into the carrier's voicemail server, no
  call placed. Legally grey area in the US (FCC has gone back and forth;
  some carriers and states treat it as a regulated robocall). **Higher
  delivery, higher legal risk.**
- **SlyDial** — actually places a call into the carrier's voicemail
  gateway via a side-channel. Similar legal stance.
- Our README explicitly chose the **Twilio AMD** approach: place a real
  call, hang up silently on humans, only play recording when AMD detects
  a machine. This is the **higher-compliance** path and the only one
  achievable without a second paid telephony API. It is the right choice
  given the design constraints in the README.

## Key technical patterns to reuse

From the Twilio docs and the code hits we examined:

1. **AMD on outbound call**: pass `machine_detection='DetectMessageEnd'`
   and an `async_amd_status_callback` URL when calling
   `client.calls.create(...)`. This makes Twilio:
   - Begin the call.
   - Run AMD in the background.
   - POST `AnsweredBy` to the async callback URL when AMD completes
     (values: `human`, `machine_start`, `machine_end_beep`,
     `machine_end_silence`, `machine_end_other`, `fax`, `unknown`).
   - In parallel, fetch TwiML from the main `url=...` to drive the call.
2. **`DetectMessageEnd` vs `Enable`**: `Enable` returns the result as
   soon as AMD decides human/machine — fast, but on machines this fires
   *before* the greeting/beep. `DetectMessageEnd` waits until the
   machine's greeting ends, so the recording starts *after* the beep —
   exactly what we want for voicemail drop. **Decision: use
   `DetectMessageEnd`.**
3. **Webhook framework**: Flask is the de-facto pairing with the Twilio
   Python SDK in 90% of examples; the README also suggests Flask.
4. **Public webhook URL during development**: ngrok / cloudflared. Not
   a project dependency — just developer tooling.

## Gaps this project must fill (i.e., why we're building it)

- HubSpot v3 contacts → call queue → outcome → activity timeline write-back.
- Timezone-aware per-contact scheduling with two attempt windows.
- Idempotent attempt tracking that survives restarts.
- Outcome-driven branching TwiML (play on machine, silent hang-up on human).
- A minimal dashboard for "who needs human follow-up today".

None of these are solved together by any open-source repo found.

## Conclusion

Build it from scratch in small Python files as the README suggests. Use
`phatjmo/vmdrop` only as a sanity-check reference for the Twilio AMD call
shape. Keep the project surface area small and obey the design
constraints (Twilio + HubSpot only, no extra APIs).
