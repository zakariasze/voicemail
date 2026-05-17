"""Minimal Flask webhook for Phase 1.

Two endpoints:

* ``POST /voice`` — TwiML returned to Twilio when the call connects.
  Phase 1 just hangs up; Phase 2 will branch on the AMD result and play
  the voicemail recording when appropriate.
* ``POST /amd`` — async Answering Machine Detection callback. Twilio
  POSTs the AMD result here (``AnsweredBy`` plus call metadata). Phase 1
  simply prints it to stdout — that print is the Phase 1 acceptance
  signal.

Run with:

    flask --app call_handler run --port 5000
"""

from __future__ import annotations

from flask import Flask, Response, request

app = Flask(__name__)


@app.post("/voice")
def voice() -> Response:
    """Return trivial TwiML so the test call disconnects cleanly.

    Phase 2 will replace this with branching that plays the voicemail
    when AMD detects a machine and hangs up silently when it detects a
    human.
    """
    twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return Response(twiml, mimetype="text/xml")


@app.post("/amd")
def amd() -> tuple[str, int]:
    """Receive Twilio's async AMD result and print it.

    See https://www.twilio.com/docs/voice/answering-machine-detection
    for the field list. The values we care about are:

    * ``CallSid``     — Twilio's identifier for the call
    * ``AnsweredBy``  — one of ``human``, ``machine_start``,
      ``machine_end_beep``, ``machine_end_silence``,
      ``machine_end_other``, ``fax``, ``unknown``
    * ``MachineDetectionDuration`` — ms AMD took to decide
    """
    call_sid = request.values.get("CallSid", "?")
    answered_by = request.values.get("AnsweredBy", "?")
    duration = request.values.get("MachineDetectionDuration", "?")
    # Phase 1 verification line — keep the format stable, the plan
    # document and the user's manual test step look for "[AMD]".
    print(
        f"[AMD] CallSid={call_sid} AnsweredBy={answered_by} "
        f"DetectionDurationMs={duration}",
        flush=True,
    )
    return ("", 204)


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    """Trivial liveness probe; handy when checking the ngrok tunnel."""
    return ("ok", 200)


if __name__ == "__main__":
    # Convenience for `python call_handler.py`; production should use
    # `flask --app call_handler run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5000)
