"""Central configuration loader.

All other modules read configuration via this module — never via
``os.environ`` directly. ``require(name)`` raises a clear error if a
required environment variable is missing, so failures happen at the
boundary instead of deep inside a Twilio call.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env once at import time. Missing .env is fine (production may use
# real environment variables instead).
load_dotenv()


def get(name: str, default: str | None = None) -> str | None:
    """Return an env var or ``default`` (``None`` by default)."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def require(name: str) -> str:
    """Return an env var or raise ``RuntimeError`` with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# --- Convenience accessors -------------------------------------------------
# Kept as functions (not module-level constants) so that import-time does
# not fail when only a subset of variables is configured — e.g. the Flask
# webhook can boot before TWILIO_* are set.

def twilio_account_sid() -> str:
    return require("TWILIO_ACCOUNT_SID")


def twilio_auth_token() -> str:
    return require("TWILIO_AUTH_TOKEN")


def twilio_from_number() -> str:
    return require("TWILIO_FROM_NUMBER")


def webhook_base_url() -> str:
    """Public HTTPS base URL Twilio can reach this server at.

    No trailing slash. Set to e.g. ``https://abcd-1234.ngrok.app`` in dev.
    """
    base = require("WEBHOOK_BASE_URL").rstrip("/")
    return base


def voicemail_recording_url() -> str | None:
    """Public URL of the voicemail .mp3. Optional in Phase 1."""
    return get("VOICEMAIL_RECORDING_URL")


def hubspot_api_key() -> str:
    """HubSpot private-app token, sent as ``Authorization: Bearer …``."""
    return require("HUBSPOT_API_KEY")


def hubspot_list_id() -> str:
    """Numeric HubSpot list ID to dial from. Returned as a string so it
    can be substituted into the URL path verbatim."""
    return require("HUBSPOT_LIST_ID")


# --- SMS follow-up (PoC) ---------------------------------------------------

_DEFAULT_SMS_BODY = (
    "Hi {first_name}, this is Kai — just left you a voicemail. "
    "Give me a call back when you have a minute. Thanks!"
)


def sms_followup_enabled() -> bool:
    """``True`` iff the post-voicemail SMS follow-up is turned on."""
    return (get("SMS_FOLLOWUP_ENABLED", "false") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def sms_followup_body() -> str:
    """Template for the follow-up SMS. Supports ``{first_name}`` /
    ``{last_name}`` placeholders, filled from the HubSpot contact row."""
    return get("SMS_FOLLOWUP_BODY", _DEFAULT_SMS_BODY) or _DEFAULT_SMS_BODY


def sms_followup_delay_seconds() -> int:
    """Seconds to wait after the call ends before sending the SMS."""
    raw = get("SMS_FOLLOWUP_DELAY_SECONDS", "15") or "15"
    try:
        return max(0, int(raw))
    except ValueError:
        return 15


# --- Call forwarding -------------------------------------------------------

def call_forwarding_enabled() -> bool:
    """``True`` iff call forwarding on human answer is turned on."""
    return (get("CALL_FORWARDING_ENABLED", "false") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def hold_recording_url() -> str | None:
    """Public URL of the hold music .mp3 played while connecting."""
    return get("HOLD_RECORDING_URL")


# --- Personalized AI intro (Phase 7) --------------------------------------

_DEFAULT_INTRO_TEMPLATE = "Hi Dr. {first_name} —"


def intro_enabled() -> bool:
    """``True`` iff the personalized AI intro is turned on."""
    return (get("INTRO_ENABLED", "false") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def elevenlabs_api_key() -> str:
    return require("ELEVENLABS_API_KEY")


def elevenlabs_voice_id() -> str:
    return require("ELEVENLABS_VOICE_ID")


def elevenlabs_model_id() -> str:
    return get("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5") or "eleven_turbo_v2_5"


def intro_template() -> str:
    """Intro template. Supports ``{first_name}`` placeholder."""
    return get("VOICEMAIL_INTRO_TEMPLATE", _DEFAULT_INTRO_TEMPLATE) or _DEFAULT_INTRO_TEMPLATE


def intro_template_version() -> str:
    """Bump to invalidate the audio cache after a template change."""
    return get("VOICEMAIL_INTRO_TEMPLATE_VERSION", "1") or "1"


def audio_cache_dir() -> str:
    """Local directory where rendered intro MP3s are stored."""
    return get("AUDIO_CACHE_DIR", "audio_cache") or "audio_cache"


def priority_numbers() -> list[str]:
    """List of up to 3 E.164 priority numbers for call forwarding.

    PRIORITY_NUMBER_1 and PRIORITY_NUMBER_2 are dialed simultaneously
    (10-second timeout). PRIORITY_NUMBER_3 is the fallback if neither
    of the first two answers (20-second timeout).
    """
    return [
        n
        for name in ("PRIORITY_NUMBER_1", "PRIORITY_NUMBER_2", "PRIORITY_NUMBER_3")
        if (n := get(name))
    ]
