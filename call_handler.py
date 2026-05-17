"""Flask webhook for outbound voicemail-drop calls.

Endpoints
---------

* ``POST /voice`` — Twilio's main TwiML webhook. With synchronous AMD
  enabled on the outbound call, Twilio holds this request until AMD
  completes and includes ``AnsweredBy`` in the form body. We branch:

  * machine_*  → ``<Play>{VOICEMAIL_RECORDING_URL}</Play><Hangup/>``
                 and log outcome ``Voicemail Left``.
  * human / unknown → ``<Hangup/>`` and log ``Human Answered``.
  * fax → ``<Hangup/>`` and log ``Failed``.

  ``unknown`` is treated as human, conservatively: better to skip a
  drop than play a recording at a real person.

* ``POST /status`` — Twilio's call ``statusCallback``. Fires once at
  the end of every call regardless of outcome. Records terminal call
  states for calls that never reached ``/voice`` (``no-answer``,
  ``busy``, ``failed``, ``canceled``). For calls that did reach
  ``/voice``, the outcome is already set; ``/status`` only fills in
  ``call_status`` (won't overwrite the outcome).

* ``GET /healthz`` — trivial liveness probe.

Run with:

    flask --app call_handler run --port 5000
"""

from __future__ import annotations

from flask import Flask, Response, request

import config
import state

app = Flask(__name__)


# Ensure the SQLite schema exists before any request is served.
state.init_db()


# --- TwiML helpers ---------------------------------------------------------

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def _twiml(body: str) -> Response:
    return Response(f"{_XML_HEADER}<Response>{body}</Response>", mimetype="text/xml")


def _play_and_hangup(recording_url: str) -> Response:
    # Escape ampersands in the URL — TwiML is XML.
    safe_url = recording_url.replace("&", "&amp;")
    return _twiml(f"<Play>{safe_url}</Play><Hangup/>")


def _silent_hangup() -> Response:
    # A 1-second pause before <Hangup/> avoids Twilio's "an application
    # error has occurred" prompt that fires when the first verb is a
    # bare <Hangup/>.
    return _twiml("<Pause length=\"1\"/><Hangup/>")


# --- AnsweredBy → outcome mapping -----------------------------------------

_MACHINE_VALUES = {
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "machine_start",
}


def _outcome_for_answered_by(answered_by: str) -> str:
    if answered_by in _MACHINE_VALUES:
        return state.OUTCOME_VOICEMAIL_LEFT
    if answered_by == "fax":
        return state.OUTCOME_FAILED
    # 'human', 'unknown', and anything unexpected: assume human.
    return state.OUTCOME_HUMAN_ANSWERED


# --- CallStatus → outcome mapping (for /status only) ----------------------

_STATUS_TO_OUTCOME = {
    "no-answer": state.OUTCOME_NO_ANSWER,
    "busy": state.OUTCOME_BUSY,
    "failed": state.OUTCOME_FAILED,
    "canceled": state.OUTCOME_FAILED,
    # 'completed' intentionally absent — /voice already set the real
    # outcome (Voicemail Left / Human Answered / Failed for fax).
}


# --- Endpoints -------------------------------------------------------------

@app.post("/voice")
def voice() -> Response:
    call_sid = request.values.get("CallSid", "")
    to_number = request.values.get("To") or request.values.get("Called") or ""
    answered_by = request.values.get("AnsweredBy", "unknown") or "unknown"

    outcome = _outcome_for_answered_by(answered_by)
    print(
        f"[voice] CallSid={call_sid} AnsweredBy={answered_by} -> {outcome}",
        flush=True,
    )

    state.record_outcome(
        call_sid,
        to_number=to_number or None,
        outcome=outcome,
        answered_by=answered_by,
    )

    if outcome == state.OUTCOME_VOICEMAIL_LEFT:
        recording_url = config.voicemail_recording_url()
        if not recording_url:
            # Misconfigured: we detected a machine but have no recording
            # to play. Hang up cleanly rather than crash the call.
            print(
                "[voice] WARNING: machine detected but "
                "VOICEMAIL_RECORDING_URL is not set; hanging up.",
                flush=True,
            )
            return _silent_hangup()
        return _play_and_hangup(recording_url)

    return _silent_hangup()


@app.post("/status")
def call_status() -> tuple[str, int]:
    call_sid = request.values.get("CallSid", "")
    to_number = request.values.get("To") or request.values.get("Called") or ""
    cs = request.values.get("CallStatus", "")

    outcome = _STATUS_TO_OUTCOME.get(cs)  # None for 'completed'
    print(
        f"[status] CallSid={call_sid} CallStatus={cs}"
        + (f" -> {outcome}" if outcome else ""),
        flush=True,
    )

    state.record_outcome(
        call_sid,
        to_number=to_number or None,
        outcome=outcome,  # None won't overwrite an existing outcome
        call_status=cs or None,
    )
    return ("", 204)


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    return ("ok", 200)


if __name__ == "__main__":
    # Convenience for `python call_handler.py`; production should use
    # `flask --app call_handler run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5000)
