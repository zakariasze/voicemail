"""ElevenLabs text-to-speech client.

A single :func:`synthesize` function that posts text to the ElevenLabs
v1 REST API and returns an MP3 byte string. Used by ``audio_cache`` to
render the personalized voicemail intro ahead of dial time.

Why direct REST instead of the official SDK: same rationale as
``hubspot_client`` — ``requests`` is already a dependency, and we only
need one endpoint.

Output format
-------------
We request ``mp3_22050_32`` (22.05 kHz mono, 32 kbps) which is plenty
for a phone call: Twilio downsamples everything to 8 kHz μ-law on the
PSTN leg anyway, and 22.05 kHz mono matches what most home-recorded
voicemails are exported at, so the seam to the human body recording
stays clean.

Voice settings
--------------
``stability=0.5`` and ``similarity_boost=0.75`` give a natural,
slightly expressive read without drifting away from the cloned voice
on retries — important since the same intro is cached and reused.
"""

from __future__ import annotations

import requests

import config

_API_BASE = "https://api.elevenlabs.io/v1"

# Phone-quality output. Higher bitrates are wasted because Twilio
# downsamples to 8 kHz μ-law before the call leaves the carrier.
_OUTPUT_FORMAT = "mp3_22050_32"

_DEFAULT_VOICE_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
}


class TTSError(RuntimeError):
    """Raised when ElevenLabs returns a non-2xx response."""


def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    model_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> bytes:
    """Render ``text`` to MP3 bytes via ElevenLabs.

    ``voice_id`` and ``model_id`` default to the values from
    ``config``. Raises :class:`TTSError` on transport or HTTP failure.
    """
    if not text or not text.strip():
        raise ValueError("text is required")

    voice = voice_id or config.elevenlabs_voice_id()
    model = model_id or config.elevenlabs_model_id()
    url = f"{_API_BASE}/text-to-speech/{voice}"

    headers = {
        "xi-api-key": config.elevenlabs_api_key(),
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": _DEFAULT_VOICE_SETTINGS,
    }
    params = {"output_format": _OUTPUT_FORMAT}

    try:
        resp = requests.post(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise TTSError(f"ElevenLabs request failed: {exc}") from exc

    if resp.status_code // 100 != 2:
        # Body may be JSON with error detail or just bytes; truncate.
        snippet = resp.text[:300] if resp.text else ""
        raise TTSError(
            f"ElevenLabs returned HTTP {resp.status_code}: {snippet}"
        )
    return resp.content
