"""Flask webhook for outbound voicemail-drop calls.

Endpoints
---------

* ``POST /voice`` — Twilio's main TwiML webhook. AMD runs in
  ``DetectMessageEnd`` mode: Twilio waits for the beep before calling
  this endpoint, so we play the recording immediately with no pause.
  We branch on ``AnsweredBy``:

  * machine_*  → ``<Play>{VOICEMAIL_RECORDING_URL}</Play><Hangup/>``
                 and log outcome ``Voicemail Left``. A follow-up SMS
                 is fired in a background thread so the TwiML response
                 stays fast.
  * human → play a short "please hold" message and simultaneously
                 ring every number in ``CLOSER_NUMBERS``. Twilio's
                 ``<Dial>`` action callback (``/transfer-status``)
                 finalizes the outcome: ``Transferred`` if a closer
                 answered, otherwise a courteous goodbye and the row
                 stays at ``Human Answered`` (we tried, we missed).
                 With no closers configured we fall back to a silent
                 hangup so the system stays useful pre-rollout.
  * fax / unknown → ``<Hangup/>`` and log accordingly.

  ``unknown`` is treated as ``No Answer``, conservatively: better to
  skip a drop than play a recording at a real person.

* ``POST /transfer-status`` — ``action`` callback on the human-pickup
  ``<Dial>``. Sets ``Transferred`` when ``DialCallStatus == completed``;
  otherwise plays the goodbye recording. Always closes the parent leg
  with ``<Hangup/>``.

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

import threading
from xml.sax.saxutils import escape as _xml_escape

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


def _xml_url(url: str) -> str:
    # Escape ampersands and other XML-significant characters in URLs.
    return _xml_escape(url)


def _play_and_hangup(recording_url: str) -> Response:
    # With DetectMessageEnd, Twilio waits for the beep before calling
    # /voice, so we play immediately with no pause needed.
    return _twiml(f"<Play>{_xml_url(recording_url)}</Play><Hangup/>")


def _silent_hangup() -> Response:
    # A 1-second pause before <Hangup/> avoids Twilio's "an application
    # error has occurred" prompt that fires when the first verb is a
    # bare <Hangup/>.
    return _twiml("<Pause length=\"1\"/><Hangup/>")


def _play_or_say(url: str | None, fallback_text: str) -> str:
    """Render a ``<Play>`` for ``url`` if set, else a ``<Say>`` fallback.

    Keeps the system usable before the team has uploaded recordings for
    the hold message and goodbye message — the call flow still works,
    just with Twilio's TTS voice instead of the polished human take.
    """
    if url:
        return f"<Play>{_xml_url(url)}</Play>"
    return f"<Say>{_xml_escape(fallback_text)}</Say>"


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
    # 'unknown': with DetectMessageEnd, Twilio does not call /voice for
    # unknown — this branch is a safety net only.
    if answered_by == "unknown":
        return state.OUTCOME_NO_ANSWER
    # 'human' and anything unexpected: a real person picked up.
    return state.OUTCOME_HUMAN_ANSWERED


# --- CallStatus → outcome mapping (for /status only) ----------------------

_STATUS_TO_OUTCOME = {
    "no-answer": state.OUTCOME_NO_ANSWER,
    "busy": state.OUTCOME_BUSY,
    "failed": state.OUTCOME_FAILED,
    "canceled": state.OUTCOME_FAILED,
    # 'completed' intentionally absent — /voice already set the real
    # outcome (Voicemail Left / Human Answered / Transferred /
    # Failed for fax).
}


# --- Background SMS follow-up ---------------------------------------------

def _send_followup_sms_async(call_sid: str, to_number: str) -> None:
    """Fire the post-voicemail SMS in a daemon thread.

    Kicked off from ``/voice`` so the TwiML response (which starts the
    recording playback) is not blocked on a Twilio REST round-trip.
    Idempotent: re-checks ``sms_sent_at`` so Twilio retries of ``/voice``
    don't double-send.
    """
    body = config.sms_followup_body()
    if not body or not to_number:
        return

    def _run() -> None:
        # Re-read the row inside the thread to catch the case where a
        # concurrent retry has already stamped sms_sent_at.
        row = state.get(call_sid)
        if row and row.get("sms_sent_at"):
            return
        try:
            import twilio_client  # local import: keeps cold start light
            sid = twilio_client.send_sms(to_number, body)
            if sid:
                state.mark_sms_sent(call_sid)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[voice] ERROR firing follow-up SMS for {call_sid}: {exc}",
                flush=True,
            )

    threading.Thread(target=_run, daemon=True).start()


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
        # Fire the SMS follow-up in the background so the recording
        # starts playing without waiting on Twilio's REST API.
        _send_followup_sms_async(call_sid, to_number)
        return _play_and_hangup(recording_url)

    if outcome == state.OUTCOME_HUMAN_ANSWERED:
        return _live_pickup_twiml()

    return _silent_hangup()


def _live_pickup_twiml() -> Response:
    """Build the TwiML for a live human pickup.

    Plays a short "please hold" message and then ``<Dial>``s every
    configured closer number simultaneously. First closer to answer
    gets bridged in; the rest are released. After the dial completes
    (success or timeout), Twilio POSTs the result to
    ``/transfer-status`` which decides the final outcome.

    If no closers are configured, we degrade to a silent hangup so
    the system stays useful before the team is wired up.
    """
    numbers = config.closer_numbers()
    if not numbers:
        print(
            "[voice] human pickup but CLOSER_NUMBERS is empty; "
            "hanging up silently.",
            flush=True,
        )
        return _silent_hangup()

    hold = _play_or_say(
        config.hold_recording_url(),
        "Please hold while we connect you.",
    )
    timeout = config.transfer_ring_timeout_seconds()
    base = config.webhook_base_url()
    action_url = f"{base}/transfer-status"
    number_tags = "".join(
        f"<Number>{_xml_escape(n)}</Number>" for n in numbers
    )
    # ringAll behavior: <Dial> with multiple <Number> children rings
    # them all in parallel by default. answerOnBridge=true keeps the
    # caller's call alive (and the recording side hears ringback) until
    # one of the closers actually answers.
    dial = (
        f'<Dial timeout="{timeout}" answerOnBridge="true" '
        f'action="{_xml_escape(action_url)}" method="POST">'
        f"{number_tags}"
        f"</Dial>"
    )
    return _twiml(hold + dial)


@app.post("/transfer-status")
def transfer_status() -> Response:
    """Action callback for the live-pickup ``<Dial>``.

    Twilio POSTs here once the dial completes. ``DialCallStatus`` tells
    us whether any closer answered:

    * ``completed`` — a closer answered and the call has now ended;
      mark the row ``Transferred`` and hang up the parent leg.
    * anything else (``no-answer``, ``busy``, ``failed``, ``canceled``)
      — nobody picked up in time; play the goodbye recording and end
      the call cleanly. The outcome stays ``Human Answered`` (we did
      reach a person, we just missed the transfer).
    """
    call_sid = request.values.get("CallSid", "")
    dial_status = request.values.get("DialCallStatus", "") or ""
    print(
        f"[transfer-status] CallSid={call_sid} DialCallStatus={dial_status}",
        flush=True,
    )

    if dial_status == "completed":
        state.record_outcome(call_sid, outcome=state.OUTCOME_TRANSFERRED)
        # Nothing more to do; the bridged call has already ended.
        return _twiml("<Hangup/>")

    goodbye = _play_or_say(
        config.goodbye_recording_url(),
        "Sorry we couldn't connect you right now. We'll try again soon. "
        "Goodbye.",
    )
    return _twiml(goodbye + "<Hangup/>")


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
