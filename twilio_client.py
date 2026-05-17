"""Place a single outbound call with Twilio Answering Machine Detection.

Phase 1 only. The only public surface is :func:`place_call`. The call
URL points at our Flask ``/voice`` endpoint (which returns a trivial
``<Hangup/>`` TwiML for now), and Twilio is asked to POST the AMD result
asynchronously to ``/amd`` where we simply print it.

Run directly as a script for the manual Phase 1 verification:

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
      voicemail greeting finishes before reporting the result, so a
      future Phase 2 playback starts after the beep instead of over the
      prompt.
    * ``async_amd_status_callback`` — Twilio POSTs ``AnsweredBy`` to our
      ``/amd`` endpoint when AMD completes, without blocking call setup.
    """
    if not to_number:
        raise ValueError("to_number is required")

    base = config.webhook_base_url()
    twiml_url = f"{base}/voice"
    amd_callback_url = f"{base}/amd"

    call = _client().calls.create(
        to=to_number,
        from_=config.twilio_from_number(),
        url=twiml_url,
        method="POST",
        machine_detection="DetectMessageEnd",
        async_amd=True,
        async_amd_status_callback=amd_callback_url,
        async_amd_status_callback_method="POST",
    )
    print(f"[twilio_client] placed call CallSid={call.sid} to={to_number}")
    return call.sid


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python twilio_client.py <E.164 number, e.g. +15551234567>")
        return 2
    place_call(argv[1])
    print(
        "[twilio_client] call placed. Watch the Flask terminal for an\n"
        "                [AMD] line showing AnsweredBy=..."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
