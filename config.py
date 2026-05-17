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
