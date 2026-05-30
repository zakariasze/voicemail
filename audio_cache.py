"""Hash-keyed cache for personalized voicemail intros.

Renders the configured intro template (e.g. ``"Hi Dr. {first_name} —"``)
once per unique first name and stores the resulting MP3 on local disk.
Subsequent contacts with the same first name reuse the cached file —
two contacts both named "John" share one rendering, so spend stays low.

Public surface
--------------
* :func:`get_or_render_intro(first_name) -> str | None` — returns a
  publicly-reachable HTTPS URL to the cached intro MP3, or ``None`` if
  the feature is disabled, the name is unusable, or rendering failed.
  Never raises — failures fall back to the generic recording.

Cache key
---------
``sha256(voice_id | model_id | template_version | normalized_first_name)``
truncated to 16 hex chars. ``template_version`` lets you invalidate
the cache after editing the script template.

Storage
-------
MP3s land in ``config.audio_cache_dir()`` (default ``audio_cache/``).
Served back to Twilio via the ``GET /audio/<filename>`` route in
``call_handler``, so no external bucket is required at PoC volume.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading

import config
import tts_client

# In-process lock to avoid two threads rendering the same name in
# parallel and double-billing the API.
_RENDER_LOCK = threading.Lock()

# Allow letters, hyphen, apostrophe, space. Anything else means the
# field is junk (emoji, "DR.", initials with periods, etc.) and we'd
# rather fall back to the generic recording than mispronounce it.
_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z\-' ]{0,30}$")


def _normalize_first_name(raw: str | None) -> str | None:
    """Return a clean first name suitable for TTS, or ``None``."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # Take the first token only — HubSpot sometimes stuffs
    # "Dr. John" or "John Q." into firstname.
    cleaned = cleaned.split()[0]
    # Strip a leading "Dr." or "Mr." just in case.
    if cleaned.lower() in {"dr.", "dr", "mr.", "mr", "ms.", "ms", "mrs.", "mrs"}:
        return None
    cleaned = cleaned.strip(".,;:")
    if not _NAME_OK.match(cleaned):
        return None
    # Title case so "JOHN" and "john" share a cache entry.
    return cleaned[:1].upper() + cleaned[1:].lower()


def _cache_key(first_name: str) -> str:
    raw = "|".join(
        [
            config.elevenlabs_voice_id(),
            config.elevenlabs_model_id(),
            config.intro_template_version(),
            config.intro_template(),
            first_name,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> str:
    return os.path.join(config.audio_cache_dir(), f"{key}.mp3")


def _public_url(key: str) -> str:
    return f"{config.webhook_base_url()}/audio/{key}.mp3"


def cache_path_for_filename(filename: str) -> str | None:
    """Resolve a public ``/audio/<filename>`` request to a local file.

    Used by ``call_handler``'s audio-serving route. Returns ``None``
    for anything that isn't a 16-hex-char ``.mp3`` so the route can't
    be tricked into serving arbitrary files.
    """
    if not re.fullmatch(r"[0-9a-f]{16}\.mp3", filename or ""):
        return None
    return os.path.join(config.audio_cache_dir(), filename)


def get_or_render_intro(first_name: str | None) -> str | None:
    """Return a public URL to the personalized intro, or ``None``.

    Never raises. ``None`` means: feature disabled, name unusable,
    config missing, or render failed — caller should fall back to the
    generic recording.
    """
    if not config.intro_enabled():
        return None

    name = _normalize_first_name(first_name)
    if not name:
        return None

    try:
        key = _cache_key(name)
    except RuntimeError as exc:
        # Missing ELEVENLABS_VOICE_ID / API key etc.
        print(f"[audio_cache] config missing, skipping intro: {exc}", flush=True)
        return None

    path = _cache_path(key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return _public_url(key)

    template = config.intro_template()
    try:
        text = template.format(first_name=name)
    except (KeyError, IndexError) as exc:
        print(
            f"[audio_cache] template error in VOICEMAIL_INTRO_TEMPLATE "
            f"({exc}); skipping intro",
            flush=True,
        )
        return None

    with _RENDER_LOCK:
        # Re-check inside the lock in case a sibling thread won the race.
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return _public_url(key)
        try:
            audio = tts_client.synthesize(text)
        except Exception as exc:  # noqa: BLE001 - never break the dial loop
            print(
                f"[audio_cache] ERROR rendering intro for {name!r}: {exc}",
                flush=True,
            )
            return None

        os.makedirs(config.audio_cache_dir(), exist_ok=True)
        # Atomic write so a crash mid-render doesn't leave a 0-byte
        # file that future calls would treat as "already cached".
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(audio)
        os.replace(tmp, path)

    print(
        f"[audio_cache] rendered intro for {name!r} -> {os.path.basename(path)} "
        f"({len(audio)} bytes)",
        flush=True,
    )
    return _public_url(key)
