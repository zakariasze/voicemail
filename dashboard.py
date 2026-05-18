"""Flask dashboard for managing call campaigns.

Phase 5. This module is read-mostly; the only writes it does are:

* create a campaign + its contacts (``POST /campaigns/``)
* update a campaign's status (``POST /campaigns/<id>/status``)

There is no authentication — same posture as ``call_handler.py`` — and
no JSON API. Templates are inline Jinja strings with autoescape on, so
CSV-pasted names cannot inject HTML.

Run with:

    flask --app dashboard run --port 5001
"""

from __future__ import annotations

import csv
import io

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

# Ensure the SQLite schema (including the Phase 5 campaign tables)
# exists before any request is served.
state.init_db()


# --- Templates -------------------------------------------------------------
# Inline Jinja strings, per the user's request. Kept short. Autoescape
# is enabled by Flask's render_template_string for ``.html``-style
# rendering, so untrusted text (campaign names, pasted contact names)
# cannot inject HTML.

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
  textarea { width: 100%; font-family: ui-monospace, monospace; }
  input[type=text] { padding: 0.3rem 0.5rem; width: 18rem; }
  .flash { background: #fff7d6; border: 1px solid #e3c200;
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
    <tr><th>Name</th><th>Status</th><th>Contacts</th><th>Created</th></tr>
  </thead>
  <tbody>
  {% for c in campaigns %}
    <tr>
      <td><a href="{{ url_for('campaign_detail', campaign_id=c.id) }}">{{ c.name }}</a></td>
      <td><span class="status-{{ c.status }}">{{ c.status }}</span></td>
      <td>{{ c.contact_count }}</td>
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
  <p>
    <label>Contacts (one per line — either <code>phone</code>
    or <code>name,phone</code>):</label><br>
    <textarea name="contacts" rows="8"
              placeholder="Alice,555-111-2222&#10;555-333-4444"></textarea>
  </p>
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

<p>Status: <span class="status-{{ campaign.status }}">{{ campaign.status }}</span></p>

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
      <td>{{ c.name or '' }}</td>
      <td>{{ c.phone }}</td>
      <td>{{ c.attempt_count }}</td>
      <td>{{ c.last_outcome or '' }}</td>
      <td class="muted">{{ c.last_call_at or '' }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="muted">This campaign has no contacts.</p>
{% endif %}
"""
)


# --- CSV parsing -----------------------------------------------------------

def _parse_contacts_csv(text: str) -> tuple[list[dict], int]:
    """Parse pasted CSV text into ``[{name, phone}]`` plus a skipped count.

    Accepts ``name,phone`` or bare ``phone`` per line. Empty lines are
    ignored. Phone numbers are normalised through
    ``hubspot_client.normalize_phone``; lines whose phone fails to
    normalise are counted in ``skipped`` and not returned.
    """
    if not text:
        return [], 0
    reader = csv.reader(io.StringIO(text))
    out: list[dict] = []
    skipped = 0
    for raw_row in reader:
        # Strip whitespace from each cell; skip empty lines.
        row = [(cell or "").strip() for cell in raw_row]
        if not any(row):
            continue
        if len(row) == 1:
            name, phone_raw = "", row[0]
        else:
            # "name,phone" — anything beyond column 2 is ignored.
            name, phone_raw = row[0], row[1]
        phone = hubspot_client.normalize_phone(phone_raw)
        if not phone:
            skipped += 1
            continue
        out.append({"name": name or None, "phone": phone})
    return out, skipped


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
    if not name:
        return redirect(
            url_for("list_campaigns", flash="Campaign name is required."),
            code=303,
        )
    contacts, skipped = _parse_contacts_csv(request.form.get("contacts") or "")
    campaign_id = state.create_campaign(name)
    inserted = state.add_campaign_contacts(campaign_id, contacts)
    flash_parts = [f"Created campaign with {inserted} contact(s)."]
    if skipped:
        flash_parts.append(f"{skipped} line(s) skipped (invalid phone).")
    flash = " ".join(flash_parts)
    return redirect(
        url_for("campaign_detail", campaign_id=campaign_id, flash=flash),
        code=303,
    )


@app.get("/campaigns/<int:campaign_id>")
def campaign_detail(campaign_id: int) -> str:
    campaign = state.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    contacts = state.list_campaign_contacts(campaign_id)
    flash = request.args.get("flash") or ""
    return render_template_string(
        _DETAIL_TMPL, campaign=campaign, contacts=contacts, flash=flash,
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
