"""Place a single outbound call with Twilio Answering Machine Detection.

The only public surface is :func:`place_call`. AMD runs synchronously
(Twilio holds the TwiML request until AMD completes), so the
``/voice`` webhook receives ``AnsweredBy`` in its POST body and can
branch in a single TwiML response — play the voicemail on a machine,
hang up silently on a human. A ``statusCallback`` is also registered
so terminal call states (no-answer, busy, failed, completed) reach
``/status`` for calls that never connect.

Run directly as a script for the manual verification step:

    python twilio_client.py +15551234567
"""

from __future__ import annotations

import sys

from twilio.rest import Client

import config


def _client() -> Client:
    return Client(config.twilio_account_sid(), config.twilio_auth_token())


def place_call(to_number: str) -> str:
    """Place one outbound call to ``to_number`` with AMD enabled.

    Returns the Twilio ``CallSid``.

    AMD configuration:

    * ``machine_detection='DetectMessageEnd'`` — wait until the
      voicemail greeting finishes before reporting the result, so the
      recording starts after the beep instead of over the prompt.
    * Synchronous AMD (no ``async_amd``): Twilio holds the TwiML
      request until AMD completes, then includes ``AnsweredBy`` in the
      POST to ``/voice``. This lets a single TwiML response branch
      between play-recording and silent-hangup.
    * ``status_callback`` on the ``completed`` event captures final
      call states for calls that never reach ``/voice`` (no-answer,
      busy, failed).
    """
    if not to_number:
        raise ValueError("to_number is required")

    base = config.webhook_base_url()
    twiml_url = f"{base}/voice"
    status_callback_url = f"{base}/status"

    call = _client().calls.create(
        to=to_number,
        from_=config.twilio_from_number(),
        url=twiml_url,
        method="POST",
        machine_detection="DetectMessageEnd",
        status_callback=status_callback_url,
        status_callback_event=["completed"],
        status_callback_method="POST",
    )
    print(f"[twilio_client] placed call CallSid={call.sid} to={to_number}")
    return call.sid


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python twilio_client.py <E.164 number, e.g. +15551234567>")
        return 2
    place_call(argv[1])
    print(
        "[twilio_client] call placed. Watch the Flask terminal for\n"
        "                [voice] (AMD outcome) and [status] (final\n"
        "                call state) lines, and check voicemail.db."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
