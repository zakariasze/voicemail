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
    # Twilio provides CallDuration in seconds on completed calls.
    duration_raw = request.values.get("CallDuration", "") or ""
    try:
        duration_seconds: float | None = float(duration_raw) if duration_raw else None
    except ValueError:
        duration_seconds = None

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

    _maybe_log_to_hubspot(call_sid, duration_seconds=duration_seconds)
    return ("", 204)


def _maybe_log_to_hubspot(
    call_sid: str,
    *,
    duration_seconds: float | None = None,
) -> None:
    """Push the finalized outcome to HubSpot, exactly once per call.

    Called from ``/status``. No-ops unless:
    * the SQLite row has both an ``outcome`` and a ``hubspot_contact_id``;
    * ``hubspot_logged_at`` is still ``NULL``.

    Failures are logged and swallowed — we return 204 to Twilio either
    way. A later reconcile job can replay un-logged rows.
    """
    row = state.get(call_sid)
    if not row:
        return
    if not row.get("hubspot_contact_id"):
        return
    if not row.get("outcome"):
        # /voice hasn't run yet (e.g. no-answer with very tight timing).
        # We still got here via /status, so ``outcome`` is in fact set
        # by record_outcome above for terminal statuses. This guard is
        # purely defensive.
        return
    if row.get("hubspot_logged_at"):
        return  # already logged; this is a Twilio retry

    try:
        # Local import keeps the Flask boot path free of the HubSpot
        # client (and of `requests`) until we actually need it.
        import hubspot_client

        hubspot_client.log_call(
            row["hubspot_contact_id"],
            outcome=row["outcome"],
            duration_seconds=duration_seconds,
        )
        state.mark_hubspot_logged(call_sid)
        print(
            f"[hubspot] logged call for contact "
            f"{row['hubspot_contact_id']}: {row['outcome']}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - we deliberately catch all
        print(
            f"[hubspot] ERROR logging call {call_sid} for contact "
            f"{row['hubspot_contact_id']}: {exc}",
            flush=True,
        )


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    return ("ok", 200)


if __name__ == "__main__":
    # Convenience for `python call_handler.py`; production should use
    # `flask --app call_handler run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5000)
