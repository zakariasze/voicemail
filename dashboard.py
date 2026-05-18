"""Flask dashboard for managing call campaigns.

A campaign is a (name, HubSpot list id) pair. Contacts are **always**
sourced live from the configured HubSpot list — there is no CSV paste,
no manual entry, and no Twilio-side contact list. The detail page
pulls the HubSpot list at request time and decorates each contact
with attempt count / last outcome / last call time looked up locally
from the ``calls`` table by phone number.

Writes this module performs:

* ``POST /campaigns/`` — create a campaign (name + list id)
* ``POST /campaigns/<id>/status`` — Start / Pause / Done

No authentication — same posture as ``call_handler.py``. Templates
are inline Jinja strings with autoescape enabled.

Run with::

    flask --app dashboard run --port 5001
"""

from __future__ import annotations

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template_string,
    request,
    url_for,
)

import hubspot_client
import state

app = Flask(__name__)

# Ensure the SQLite schema (including the Phase 5 campaigns table)
# exists before any request is served.
state.init_db()


# --- Templates -------------------------------------------------------------
# Inline Jinja strings. Autoescape is on, so untrusted text (HubSpot
# names / phones, campaign names) cannot inject HTML.

_BASE_CSS = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem;
         color: #222; }
  h1 { margin-top: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { border-bottom: 1px solid #ddd; padding: 0.5rem 0.75rem;
           text-align: left; vertical-align: top; }
  th { background: #f6f6f6; }
  .status-active { color: #0a7d2c; font-weight: 600; }
  .status-paused { color: #b35900; font-weight: 600; }
  .status-done   { color: #555;    font-weight: 600; }
  .muted { color: #888; }
  form.inline { display: inline; }
  button { padding: 0.4rem 0.9rem; margin-right: 0.4rem;
           border: 1px solid #aaa; background: #fafafa;
           border-radius: 4px; cursor: pointer; }
  button:hover { background: #eee; }
  input[type=text] { padding: 0.3rem 0.5rem; width: 18rem; }
  .flash { background: #fff7d6; border: 1px solid #e3c200;
           padding: 0.5rem 0.75rem; border-radius: 4px;
           margin: 0.75rem 0; }
  .error { background: #ffe0e0; border: 1px solid #e36060;
           padding: 0.5rem 0.75rem; border-radius: 4px;
           margin: 0.75rem 0; }
  nav { margin-bottom: 1rem; font-size: 0.9rem; }
  nav a { color: #0366d6; text-decoration: none; }
  nav a:hover { text-decoration: underline; }
</style>
"""

_LIST_TMPL = (
    _BASE_CSS
    + """
<nav><a href="{{ url_for('list_campaigns') }}">campaigns</a></nav>
<h1>Campaigns</h1>

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

{% if campaigns %}
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>HubSpot list</th>
      <th>Status</th>
      <th>Created</th>
    </tr>
  </thead>
  <tbody>
  {% for c in campaigns %}
    <tr>
      <td><a href="{{ url_for('campaign_detail', campaign_id=c.id) }}">{{ c.name }}</a></td>
      <td><code>{{ c.hubspot_list_id }}</code></td>
      <td><span class="status-{{ c.status }}">{{ c.status }}</span></td>
      <td class="muted">{{ c.created_at }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">No campaigns yet.</p>
{% endif %}

<h2>New campaign</h2>
<form method="post" action="{{ url_for('create_campaign') }}">
  <p><label>Name <input type="text" name="name" required></label></p>
  <p><label>HubSpot list ID
    <input type="text" name="hubspot_list_id" required></label></p>
  <p><button type="submit">Create campaign</button></p>
</form>
"""
)

_DETAIL_TMPL = (
    _BASE_CSS
    + """
<nav><a href="{{ url_for('list_campaigns') }}">&larr; campaigns</a></nav>
<h1>{{ campaign.name }}</h1>

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
{% if error %}<div class="error">{{ error }}</div>{% endif %}

<p>
  HubSpot list: <code>{{ campaign.hubspot_list_id }}</code><br>
  Status: <span class="status-{{ campaign.status }}">{{ campaign.status }}</span>
</p>

<p>
  <form class="inline" method="post"
        action="{{ url_for('set_status', campaign_id=campaign.id) }}">
    <input type="hidden" name="status" value="active">
    <button type="submit" {% if campaign.status == 'active' %}disabled{% endif %}>Start</button>
  </form>
  <form class="inline" method="post"
        action="{{ url_for('set_status', campaign_id=campaign.id) }}">
    <input type="hidden" name="status" value="paused">
    <button type="submit" {% if campaign.status == 'paused' %}disabled{% endif %}>Pause</button>
  </form>
  <form class="inline" method="post"
        action="{{ url_for('set_status', campaign_id=campaign.id) }}">
    <input type="hidden" name="status" value="done">
    <button type="submit" {% if campaign.status == 'done' %}disabled{% endif %}>Done</button>
  </form>
</p>

{% if contacts %}
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>Phone</th>
      <th>Attempts</th>
      <th>Last outcome</th>
      <th>Last call time</th>
    </tr>
  </thead>
  <tbody>
  {% for c in contacts %}
    <tr>
      <td>{{ c.name }}</td>
      <td>{{ c.phone or '' }}{% if c.phone_raw and not c.phone %}<span class="muted"> ({{ c.phone_raw }} — unparsed)</span>{% endif %}</td>
      <td>{{ c.attempt_count }}</td>
      <td>{{ c.last_outcome or '' }}</td>
      <td class="muted">{{ c.last_call_at or '' }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% elif not error %}
<p class="muted">This HubSpot list has no contacts.</p>
{% endif %}
"""
)


# --- Helpers ---------------------------------------------------------------

def _decorate_contacts(raw_contacts: list[dict]) -> list[dict]:
    """Normalise + decorate HubSpot contacts with local call stats.

    Each row in the output has:
    ``name``, ``phone`` (E.164 or ``None``), ``phone_raw``,
    ``attempt_count``, ``last_outcome``, ``last_call_at``.
    """
    rows: list[dict] = []
    for c in raw_contacts:
        first = (c.get("firstname") or "").strip()
        last = (c.get("lastname") or "").strip()
        name = (first + " " + last).strip() or "(no name)"
        phone_raw = c.get("phone")
        phone = hubspot_client.normalize_phone(phone_raw)
        rows.append({
            "name": name,
            "phone": phone,
            "phone_raw": phone_raw,
            "attempt_count": 0,
            "last_outcome": None,
            "last_call_at": None,
        })
    phones = [r["phone"] for r in rows if r["phone"]]
    if phones:
        stats = state.phone_call_stats(phones)
        for r in rows:
            if r["phone"] and r["phone"] in stats:
                s = stats[r["phone"]]
                r["attempt_count"] = s["attempt_count"]
                r["last_outcome"] = s["last_outcome"]
                r["last_call_at"] = s["last_call_at"]
    return rows


# --- Routes ----------------------------------------------------------------

@app.get("/")
def index() -> Response:
    return redirect(url_for("list_campaigns"), code=302)


@app.get("/campaigns/")
def list_campaigns() -> str:
    campaigns = state.list_campaigns()
    flash = request.args.get("flash") or ""
    return render_template_string(
        _LIST_TMPL, campaigns=campaigns, flash=flash,
    )


@app.post("/campaigns/")
def create_campaign() -> Response:
    name = (request.form.get("name") or "").strip()
    list_id = (request.form.get("hubspot_list_id") or "").strip()
    if not name or not list_id:
        return redirect(
            url_for(
                "list_campaigns",
                flash="Both a campaign name and a HubSpot list ID are required.",
            ),
            code=303,
        )
    campaign_id = state.create_campaign(name, list_id)
    return redirect(
        url_for(
            "campaign_detail",
            campaign_id=campaign_id,
            flash=f"Created campaign sourcing from HubSpot list {list_id}.",
        ),
        code=303,
    )


@app.get("/campaigns/<int:campaign_id>")
def campaign_detail(campaign_id: int) -> str:
    campaign = state.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    flash = request.args.get("flash") or ""
    error = ""
    contacts: list[dict] = []
    try:
        raw = hubspot_client.list_contacts(campaign["hubspot_list_id"])
        contacts = _decorate_contacts(raw)
    except Exception as exc:  # noqa: BLE001 - dashboard must never 500 here
        # Log the full exception for the operator (single-user tool,
        # operator watches the terminal) but show a generic message to
        # the page so HubSpot response bodies don't leak into the UI.
        print(
            f"[dashboard] ERROR loading HubSpot list "
            f"{campaign['hubspot_list_id']!r}: {exc}",
            flush=True,
        )
        error = (
            f"Could not load HubSpot list {campaign['hubspot_list_id']!r}. "
            f"Check the list ID and the server logs for details."
        )
    return render_template_string(
        _DETAIL_TMPL,
        campaign=campaign,
        contacts=contacts,
        flash=flash,
        error=error,
    )


@app.post("/campaigns/<int:campaign_id>/status")
def set_status(campaign_id: int) -> Response:
    if not state.get_campaign(campaign_id):
        abort(404)
    new_status = (request.form.get("status") or "").strip()
    if new_status not in state.CAMPAIGN_STATUSES:
        abort(400)
    state.set_campaign_status(campaign_id, new_status)
    return redirect(
        url_for("campaign_detail", campaign_id=campaign_id),
        code=303,
    )


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    return ("ok", 200)


if __name__ == "__main__":
    # Convenience for `python dashboard.py`; production should use
    # `flask --app dashboard run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5001)
