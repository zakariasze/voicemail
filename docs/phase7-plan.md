# Phase 7 — Personalized AI voicemail intro

## Goal

Replace the generic `"Hi, this is Kai…"` opener with a per-contact AI-rendered
`"Hi Dr. {first_name} —"` that flows seamlessly into the existing
human-recorded voicemail body. No live agent, no streaming, no LLM in the
hot path — just one extra `<Play>` verb.

## Design

Two `<Play>` verbs back-to-back:

```xml
<Play>{personalized_intro_url}</Play>
<Play>{VOICEMAIL_RECORDING_URL}</Play>
<Hangup/>
```

Twilio plays adjacent `<Play>`s gaplessly. The intro is rendered ahead of
dial time, cached on local disk keyed by first name, and served back to
Twilio from the same Flask app under `GET /audio/<hash>.mp3`.

### Components

- **`tts_client.synthesize(text)`** — single-call ElevenLabs v1 REST wrapper.
  Returns MP3 bytes. Output format `mp3_22050_32` (22.05 kHz mono, 32 kbps)
  matches typical home-recorded voicemail bodies and is well above the 8 kHz
  PSTN ceiling. Voice settings `stability=0.5`, `similarity_boost=0.75`,
  `use_speaker_boost=true` for a natural read that stays close to the cloned
  voice across cache hits.
- **`audio_cache.get_or_render_intro(first_name)`** — returns a public URL or
  `None`. Hash key is
  `sha256(voice_id|model_id|template_version|template|first_name)[:16]`.
  Two contacts with the same first name share one render. Threadsafe via an
  in-process `threading.Lock` to prevent a duplicate render race. Atomic
  write (`*.tmp` → `os.replace`) so a crash mid-render can't leave a 0-byte
  file. Never raises — `None` means "fall back to generic recording".
- **`scheduler.run_once`** — calls `audio_cache.get_or_render_intro` for each
  contact about to be dialed and passes the URL to `twilio_client.place_call`.
- **`twilio_client.place_call`** — accepts `intro_audio_url`, persists it on
  the calls row via `state.record_call_placed`.
- **`state.calls.intro_audio_url`** — new TEXT column added via
  `_MIGRATIONS` so existing DBs upgrade in place. Threaded through
  `record_outcome` upsert with the same "don't overwrite with NULL" semantics
  as every other field.
- **`call_handler.voice`** — when the row has an `intro_audio_url`, emits two
  `<Play>`s; otherwise emits one (existing behavior, untouched).
- **`call_handler.serve_audio`** — `GET /audio/<filename>` reads from
  `AUDIO_CACHE_DIR` and returns `audio/mpeg`. Filename pattern is locked to
  `[0-9a-f]{16}\.mp3` via `audio_cache.cache_path_for_filename` so the route
  can't be coerced into serving arbitrary files.

### Name normalization

`audio_cache._normalize_first_name`:

- Empty / whitespace → `None`.
- Takes the first whitespace-separated token (HubSpot sometimes stuffs
  `"Dr. John"` or `"John Q."` into firstname).
- Drops bare salutations (`"Dr."`, `"Mr."`, `"Ms."`, `"Mrs."`).
- Strips trailing punctuation.
- Allows letters, hyphen, apostrophe, space; rejects digits, emoji, anything
  else.
- Title-cases so `"JOHN"` and `"john"` share a cache entry.

Anything that doesn't pass yields `None`, which falls back to the generic
recording. Better to skip personalization than mispronounce.

### Why the seam is invisible

The plan's three knobs:

1. **Same voice on both clips.** Clone the voice from the existing
   `VOICEMAIL_RECORDING_URL` via ElevenLabs Instant Voice Clone, set the
   resulting voice id as `ELEVENLABS_VOICE_ID`. This is the single biggest
   factor.
2. **Matched audio format.** `mp3_22050_32` mono on the intro side; ensure
   the human body is also 22.05 kHz mono MP3 (re-encode once if needed).
   Twilio downsamples to 8 kHz μ-law on PSTN regardless, so any extra
   bitrate is wasted.
3. **Loudness + room tone.** Peak-normalize both clips to ~−3 dBFS (or
   −16 LUFS); end the intro with a 150–250 ms silent tail so it doesn't
   crash into the body. Optionally prepend 100–200 ms of room tone from
   the body to the intro so the noise floor is continuous.

### Script seam

Default template: `"Hi Dr. {first_name} —"`. The em-dash gives ElevenLabs a
natural pitch fall and short pause that masks any micro-discontinuity at the
boundary. The body recording should not also start with `"Hi"` — trim the
generic greeting off it once so the AI intro becomes the new opener.

### Cost

ElevenLabs Creator at $22/mo gives ~100k characters. The intro is ~25
characters per render. Even with thousands of unique first names, the cache
keeps spend at fractions of a penny per contact — and re-attempts cost
nothing because the same MP3 is reused.

### Fallback path

`intro_audio_url` is purely additive. Any of the following falls through to
the existing single-`<Play>` path with zero call-flow change:

- `INTRO_ENABLED=false`
- `firstname` empty / junky / a salutation
- ElevenLabs config missing
- TTS request fails or times out (caught + logged, returns `None`)
- Existing rows from before the migration (`intro_audio_url IS NULL`)

## Config

| Var | Required | Default | Notes |
| --- | --- | --- | --- |
| `INTRO_ENABLED` | no | `false` | Master switch. |
| `ELEVENLABS_API_KEY` | when enabled | — | Sent as `xi-api-key` header. |
| `ELEVENLABS_VOICE_ID` | when enabled | — | The cloned/chosen voice. |
| `ELEVENLABS_MODEL_ID` | no | `eleven_turbo_v2_5` | Turbo is cheap & fast for batch render. |
| `VOICEMAIL_INTRO_TEMPLATE` | no | `Hi Dr. {first_name} —` | `{first_name}` only. |
| `VOICEMAIL_INTRO_TEMPLATE_VERSION` | no | `1` | Bump to invalidate cache. |
| `AUDIO_CACHE_DIR` | no | `audio_cache` | Must be writable by the Flask process. |

## Tuning checklist (operator-facing)

1. Clone the voice from the existing `VOICEMAIL_RECORDING_URL` in ElevenLabs;
   record the voice id.
2. Set `INTRO_ENABLED=true`, `ELEVENLABS_*`, restart scheduler + Flask.
3. Trigger a single dial to your own phone with a HubSpot contact that has
   `firstname` set. Listen on a real phone (PSTN downsampling is the actual
   quality bar — don't judge from headphones).
4. If there's an audible click at the seam → re-export the body MP3 at
   22.05 kHz mono.
5. If the level jumps → peak-normalize both clips to the same target.
6. If the timbre changes → re-clone the voice with a longer / cleaner sample.
7. If an uncommon name is mispronounced → either accept it, or hand-curate
   `firstname` for that contact in HubSpot, or extend `_NAME_OK` /
   `_normalize_first_name` to reject the pattern so it falls back to generic.
8. Bump `VOICEMAIL_INTRO_TEMPLATE_VERSION` after editing the template — it's
   part of the cache key, so the next render is fresh; old MP3s become
   garbage and can be deleted from `AUDIO_CACHE_DIR` at any time.

## What this does NOT do

- No live LLM, no live TTS streaming, no `<Connect><Stream>`, no real-time
  agent, no WebSocket server.
- No change to AMD, `/status`, HubSpot logging, SMS follow-up, call
  forwarding, or the dashboard.
- No new vendor besides ElevenLabs. Storage is local disk; if volume grows,
  swap `audio_cache.py` to S3/R2 with a one-line URL change.
