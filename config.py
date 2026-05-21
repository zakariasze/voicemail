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


# --- Live-pickup / transfer to closers -------------------------------------
# When AMD reports ``human``, ``/voice`` plays a short hold message and
# simultaneously dials every number in ``closer_numbers()``. First closer
# to pick up gets the call; the rest are released. If nobody answers
# within ``transfer_ring_timeout_seconds()`` we play a courteous goodbye
# and hang up cleanly.

def closer_numbers() -> list[str]:
    """Comma-separated E.164 numbers to ring on a live human pickup.

    Returns an empty list when unset, in which case ``/voice`` falls back
    to the silent-hangup behavior — the system stays useful even before
    the closer team is wired up.
    """
    raw = get("CLOSER_NUMBERS", "") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def hold_recording_url() -> str | None:
    """Public URL of the short 'Please hold…' .mp3 to play to a human.

    Optional: when unset, ``/voice`` uses a built-in ``<Say>`` fallback.
    """
    return get("HOLD_RECORDING_URL")


def goodbye_recording_url() -> str | None:
    """Public URL of the courteous goodbye .mp3 used when no closer
    picks up within the ring window. Optional: ``<Say>`` fallback."""
    return get("GOODBYE_RECORDING_URL")


def transfer_ring_timeout_seconds() -> int:
    """Seconds the closers' phones ring before we give up and hang up.

    Per the workflow overview: ~20 seconds. Configurable so the team can
    tune it without a code change.
    """
    raw = get("TRANSFER_RING_TIMEOUT_SECONDS", "20") or "20"
    try:
        return max(1, int(raw))
    except ValueError:
        return 20


# --- Voicemail follow-up SMS -----------------------------------------------

def sms_followup_body() -> str:
    """Body of the SMS sent automatically after a voicemail is left.

    Falls back to a short generic message so the system stays useful
    out of the box.
    """
    return get(
        "SMS_FOLLOWUP_BODY",
        "Hi — we just left you a voicemail. Reply or call back when you "
        "get a chance, thanks!",
    ) or ""


def hubspot_api_key() -> str:
    """HubSpot private-app token, sent as ``Authorization: Bearer …``."""
    return require("HUBSPOT_API_KEY")


def hubspot_list_id() -> str:
    """Numeric HubSpot list ID to dial from. Returned as a string so it
    can be substituted into the URL path verbatim."""
    return require("HUBSPOT_LIST_ID")
