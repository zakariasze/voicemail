"""Flask webhook for outbound voicemail-drop calls.

Endpoints
---------

* ``POST /voice`` — Twilio's main TwiML webhook. AMD runs in
  ``DetectMessageEnd`` mode: Twilio waits for the beep before calling
  this endpoint, so we play the recording immediately with no pause.
  We branch on ``AnsweredBy``:

  * machine_*  → ``<Play>{VOICEMAIL_RECORDING_URL}</Play><Hangup/>``
                 and log outcome ``Voicemail Left``.
  * human → ``<Hangup/>`` and log ``Human Answered``.
  * fax / unknown → ``<Hangup/>`` and log accordingly.

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

import threading

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


def _play_and_hangup(recording_url: str, *, intro_url: str | None = None) -> Response:
    # Escape ampersands in the URL — TwiML is XML.
    safe_url = recording_url.replace("&", "&amp;")
    # With DetectMessageEnd, Twilio waits for the beep before calling
    # /voice, so we play immediately with no pause needed.
    #
    # If a personalized AI intro was rendered for this call, play it
    # first. Twilio plays adjacent <Play> verbs back-to-back with no
    # gap, so the seam is invisible when the intro and body share the
    # same voice + audio format.
    if intro_url:
        safe_intro = intro_url.replace("&", "&amp;")
        return _twiml(
            f"<Play>{safe_intro}</Play><Play>{safe_url}</Play><Hangup/>"
        )
    return _twiml(f"<Play>{safe_url}</Play><Hangup/>")


def _silent_hangup() -> Response:
    # A 1-second pause before <Hangup/> avoids Twilio's "an application
    # error has occurred" prompt that fires when the first verb is a
    # bare <Hangup/>.
    return _twiml("<Pause length=\"1\"/><Hangup/>")


def _forward_twiml(call_sid: str) -> Response:  # noqa: ARG001 — reserved for future per-call state
    """Return forwarding TwiML for a human-answered call.

    Dials PRIORITY_NUMBER_1 first (20s ring window). If P1 doesn't
    answer, ``/forward-status?attempt=1`` falls through to
    PRIORITY_NUMBER_2, and then PRIORITY_NUMBER_3 (if configured).
    Sequential dialling avoids races where a call-screening service on
    one number auto-answers and cancels the others before they can
    even ring.

    Falls back to a silent hangup if the feature is disabled or any
    required config is missing.
    """
    if not config.call_forwarding_enabled():
        print("[forward] call forwarding disabled; hanging up.", flush=True)
        return _silent_hangup()

    hold_url = config.hold_recording_url()
    numbers = config.priority_numbers()
    print(
        f"[forward] CallSid={call_sid} initiating forward — "
        f"priority_numbers={numbers} hold_url={hold_url}",
        flush=True,
    )

    if not hold_url:
        print(
            "[forward] WARNING: CALL_FORWARDING_ENABLED is set but "
            "HOLD_RECORDING_URL is not configured; hanging up.",
            flush=True,
        )
        return _silent_hangup()

    if len(numbers) < 1:
        print(
            "[forward] WARNING: CALL_FORWARDING_ENABLED is set but no "
            "PRIORITY_NUMBER_* values are configured; hanging up.",
            flush=True,
        )
        return _silent_hangup()

    base = config.webhook_base_url()
    safe_hold = hold_url.replace("&", "&amp;")
    whisper_url = f"{base}/forward-whisper"
    dial = (
        f'<Dial action="{base}/forward-status?attempt=1" '
        f'timeout="20" answerOnBridge="true">'
        f'<Number url="{whisper_url}">{numbers[0]}</Number>'
        f"</Dial>"
    )
    print(
        f"[forward] CallSid={call_sid} dialling P1={numbers[0]} "
        f"(20s timeout, sequential)",
        flush=True,
    )
    return _twiml(f"<Play>{safe_hold}</Play>{dial}")


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
    # 'human' and anything unexpected: hang up silently.
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
        # Look up the personalized intro URL stashed at placement time.
        # Falls back to body-only if the row is missing it (intro
        # disabled, name unusable, render failed, or this call wasn't
        # placed by us).
        row = state.get(call_sid) or {}
        intro_url = row.get("intro_audio_url")
        if intro_url:
            print(
                f"[voice] CallSid={call_sid} playing personalized intro "
                f"+ body",
                flush=True,
            )
        return _play_and_hangup(recording_url, intro_url=intro_url)

    # Anything not classified as a machine (human / unknown / fax) is
    # forwarded. The press-to-accept whisper in /forward-whisper
    # protects against screening services and voicemail systems
    # auto-answering the dial legs.
    return _forward_twiml(call_sid)


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
    _maybe_schedule_followup_sms(call_sid)
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


def _maybe_schedule_followup_sms(call_sid: str) -> None:
    """Schedule a delayed SMS follow-up if the call ended in Voicemail Left.

    PoC implementation: spawns a daemon ``threading.Timer`` that fires
    once after ``SMS_FOLLOWUP_DELAY_SECONDS``. Idempotency is enforced
    by the ``sms_sent_at`` column — if Twilio retries ``/status`` we
    re-schedule, but ``_send_followup_sms`` re-checks the row before
    actually sending. A Flask restart during the delay window will lose
    the timer; see ``PRODUCTION_MIGRATION.md`` for the durable design.
    """
    if not config.sms_followup_enabled():
        return
    row = state.get(call_sid)
    if not row:
        return
    if row.get("outcome") != state.OUTCOME_VOICEMAIL_LEFT:
        return
    if row.get("sms_sent_at"):
        return
    to_number = row.get("to_number")
    if not to_number:
        print(f"[sms] no to_number on row {call_sid}; skipping", flush=True)
        return

    first_name = ""
    last_name = ""
    contact_id = row.get("hubspot_contact_id")
    if contact_id:
        try:
            import hubspot_client
            contact = hubspot_client.get_contact(str(contact_id))
            first_name = contact.get("firstname") or ""
            last_name = contact.get("lastname") or ""
        except Exception as exc:  # noqa: BLE001
            print(
                f"[sms] could not fetch contact {contact_id} for name "
                f"substitution: {exc}",
                flush=True,
            )

    template = config.sms_followup_body()
    try:
        body = template.format(first_name=first_name, last_name=last_name)
    except (KeyError, IndexError) as exc:
        print(
            f"[sms] template error in SMS_FOLLOWUP_BODY ({exc}); "
            f"sending template verbatim",
            flush=True,
        )
        body = template

    delay = config.sms_followup_delay_seconds()
    print(
        f"[sms] scheduling follow-up for {call_sid} -> {to_number} "
        f"in {delay}s",
        flush=True,
    )
    timer = threading.Timer(
        delay,
        _send_followup_sms,
        args=(call_sid, to_number, body),
    )
    timer.daemon = True
    timer.start()


def _send_followup_sms(call_sid: str, to_number: str, body: str) -> None:
    """Send the follow-up SMS, then stamp ``sms_sent_at``."""
    row = state.get(call_sid)
    if not row:
        return
    if row.get("sms_sent_at"):
        return  # another fire already won
    try:
        import twilio_client
        twilio_client.send_sms(to_number, body)
        state.mark_sms_sent(call_sid)
        print(f"[sms] sent follow-up for {call_sid} to {to_number}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[sms] ERROR sending follow-up for {call_sid} to {to_number}: "
            f"{exc}",
            flush=True,
        )


def _e164(n: str) -> str:
    n = (n or "").strip().lstrip("+")
    if len(n) == 10:
        n = "1" + n
    return n


def _xml_escape(text: str) -> str:
    """Escape XML special chars for safe embedding inside TwiML text."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _lookup_contact_name(parent_call_sid: str, log_prefix: str) -> str:
    """Return the HubSpot contact name for the parent call, or "".

    Resolves ParentCallSid → state row → hubspot_contact_id →
    ``hubspot_client.get_contact``. Any failure (missing row, missing
    contact id, HubSpot error) yields an empty string; callers decide
    on a fallback phrasing.
    """
    if not parent_call_sid:
        return ""
    row = state.get(parent_call_sid)
    if not row:
        print(
            f"{log_prefix} no state row found for ParentCallSid={parent_call_sid}",
            flush=True,
        )
        return ""
    contact_id = row.get("hubspot_contact_id")
    if not contact_id:
        print(f"{log_prefix} no hubspot_contact_id on row", flush=True)
        return ""
    try:
        import hubspot_client
        contact = hubspot_client.get_contact(str(contact_id))
    except Exception as exc:  # noqa: BLE001
        print(
            f"{log_prefix} could not fetch HubSpot contact {contact_id}: {exc}",
            flush=True,
        )
        return ""
    first_name = contact.get("firstname") or ""
    last_name = contact.get("lastname") or ""
    name = f"{first_name} {last_name}".strip()
    print(
        f"{log_prefix} contact fetched — firstname={first_name!r} "
        f"lastname={last_name!r} name={name!r}",
        flush=True,
    )
    return name


@app.post("/forward-whisper")
def forward_whisper() -> Response:
    """Press-to-accept whisper played to each <Number> leg on answer.

    The leg must press any digit within 10s to actually take the call.
    Voicemail systems and screening services (e.g. Google Voice "say
    your name") will never press a key, so they fall through to
    ``<Hangup/>`` and the parent ``<Dial>`` continues ringing the other
    legs or times out — instead of bridging the contact into a
    screening prompt.

    The whisper also announces the HubSpot contact name (looked up via
    the parent CallSid → state row → hubspot_contact_id) so the
    priority number knows who is calling before accepting.
    """
    called = request.values.get("Called", "") or request.values.get("To", "")
    parent_call_sid = request.values.get("ParentCallSid", "")
    caller = request.values.get("Caller", "")
    call_sid = request.values.get("CallSid", "")
    print(
        f"[whisper] CallSid={call_sid} ParentCallSid={parent_call_sid} "
        f"Called={called} Caller={caller}",
        flush=True,
    )

    name = _lookup_contact_name(parent_call_sid, "[whisper]")
    if name:
        prompt = f"Incoming forwarded call from {_xml_escape(name)}. Press any key to accept."
    else:
        prompt = "Incoming forwarded call. Press any key to accept."

    base = config.webhook_base_url()
    # Pass the called number + parent through so /forward-accept knows
    # which leg confirmed and can fire the P2 SMS when P1 accepts.
    accept_url = (
        f"{base}/forward-accept"
        f"?called={_e164(called)}"
        f"&parent={parent_call_sid}"
    )
    body = (
        f'<Gather numDigits="1" timeout="10" action="{accept_url}" method="POST">'
        f"<Say>{prompt}</Say>"
        f"</Gather>"
        f"<Hangup/>"
    )
    return _twiml(body)


@app.post("/forward-accept")
def forward_accept() -> Response:
    """Confirmation endpoint hit when the agent presses a key.

    Returning an empty ``<Response>`` lets Twilio bridge this leg to
    the parent call. If no digits arrived (defensive — Twilio normally
    falls through to ``<Hangup/>`` in /forward-whisper instead of
    calling this URL on timeout), we hang up the leg.

    Also: if the leg that just confirmed is P1, send the notification
    SMS to P2 here (moved out of /forward-whisper so screening systems
    that auto-answer can't trigger the SMS).
    """
    digits = request.values.get("Digits", "") or ""
    called_norm = request.args.get("called", "") or ""
    parent_call_sid = request.args.get("parent", "") or ""
    call_sid = request.values.get("CallSid", "")
    print(
        f"[accept] CallSid={call_sid} ParentCallSid={parent_call_sid} "
        f"called={called_norm} digits={digits!r}",
        flush=True,
    )

    if not digits:
        print("[accept] no digits received; hanging up this leg.", flush=True)
        return _twiml("<Hangup/>")

    numbers = config.priority_numbers()
    if numbers and parent_call_sid:
        p1_norm = _e164(numbers[0])
        if called_norm == p1_norm and len(numbers) >= 2:
            print(
                f"[accept] P1 accepted — sending notification SMS to "
                f"P2={numbers[1]}",
                flush=True,
            )
            _send_forward_notification_sms(parent_call_sid, numbers[1])
        else:
            print("[accept] non-P1 leg accepted; no SMS sent.", flush=True)

    # Empty response → leg continues, bridge completes.
    return _twiml("")


def _send_forward_notification_sms(parent_call_sid: str, to_number: str) -> None:
    """Send an SMS to PRIORITY_NUMBER_2 when PRIORITY_NUMBER_1 picks up."""
    print(
        f"[forward-notify] looking up contact for ParentCallSid={parent_call_sid}",
        flush=True,
    )
    name = _lookup_contact_name(parent_call_sid, "[forward-notify]") or "unknown contact"
    body = f"Priority 1 picked up — call is with {name}."

    # Normalize bare 10-digit number to E.164
    raw_to = to_number
    if not to_number.startswith("+"):
        to_number = "+1" + to_number.lstrip("1") if len(to_number) == 10 else "+" + to_number
    print(
        f"[forward-notify] sending SMS to P2 — raw={raw_to!r} e164={to_number} body={body!r}",
        flush=True,
    )

    try:
        import twilio_client
        sid = twilio_client.send_sms(to_number, body)
        print(f"[forward-notify] SMS sent OK sid={sid} to={to_number}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[forward-notify] ERROR sending SMS to {to_number}: {exc}", flush=True)


@app.post("/forward-status")
def forward_status() -> Response:
    """Twilio ``<Dial action=…>`` callback — fires when the dial attempt ends.

    Query parameter ``attempt`` (1 or 2) tracks which stage we're in:

    * **attempt=1**: PRIORITY_NUMBER_1 and PRIORITY_NUMBER_2 were dialled
      simultaneously for 10 seconds.
      - If one answered (``completed``): record and hang up.
      - If neither answered: issue a second ``<Dial>`` for PRIORITY_NUMBER_3.
    * **attempt=2**: PRIORITY_NUMBER_3 attempt is done. Record and hang up
      regardless of outcome.
    """
    call_sid    = request.values.get("CallSid", "")
    dial_status = request.values.get("DialCallStatus", "")
    dial_to     = request.values.get("DialTo") or None
    attempt     = request.args.get("attempt", "1")

    print(
        f"[forward] CallSid={call_sid} attempt={attempt} "
        f"DialCallStatus={dial_status} DialTo={dial_to}",
        flush=True,
    )

    if dial_status == "completed":
        print(
            f"[forward] CallSid={call_sid} attempt={attempt} bridge completed — "
            f"answered by DialTo={dial_to}; recording result.",
            flush=True,
        )
        state.mark_forwarded(call_sid, forwarded_to=dial_to, forward_status=dial_status)
        return _twiml("")

    numbers = config.priority_numbers()
    base = config.webhook_base_url()
    whisper_url = f"{base}/forward-whisper"

    # Sequential cascade: attempt 1 just finished P1, try P2 next; etc.
    try:
        attempt_num = int(attempt)
    except ValueError:
        attempt_num = 1
    next_idx = attempt_num  # attempt 1 finished -> try index 1 (P2)

    if next_idx < len(numbers):
        next_attempt = attempt_num + 1
        dial = (
            f'<Dial action="{base}/forward-status?attempt={next_attempt}" '
            f'timeout="20" answerOnBridge="true">'
            f'<Number url="{whisper_url}">{numbers[next_idx]}</Number>'
            f"</Dial>"
        )
        print(
            f"[forward] CallSid={call_sid} attempt={attempt} no answer "
            f"(DialCallStatus={dial_status}) — trying "
            f"P{next_idx + 1}={numbers[next_idx]}",
            flush=True,
        )
        return _twiml(dial)

    print(
        f"[forward] CallSid={call_sid} attempt={attempt} — no further "
        f"priority numbers configured; giving up.",
        flush=True,
    )
    state.mark_forwarded(call_sid, forwarded_to=None, forward_status=dial_status)
    return _twiml("")


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    return ("ok", 200)


@app.get("/audio/<path:filename>")
def serve_audio(filename: str):
    """Serve a cached personalized-intro MP3 to Twilio.

    ``audio_cache.cache_path_for_filename`` validates that ``filename``
    matches the 16-hex-char ``.mp3`` pattern produced by the cache key
    hash, so this route can't be tricked into serving arbitrary files.
    """
    import audio_cache as _audio_cache

    path = _audio_cache.cache_path_for_filename(filename)
    if not path or not __import__("os").path.exists(path):
        return ("not found", 404)
    # Stream the bytes back. We don't use Flask's send_from_directory
    # because the cache dir is configurable and may be relative to CWD;
    # reading the bytes directly keeps the path-handling explicit.
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(data, mimetype="audio/mpeg")


if __name__ == "__main__":
    # Convenience for `python call_handler.py`; production should use
    # `flask --app call_handler run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5000)
