"""Flask dashboard for managing call campaigns.

A campaign is a (name, HubSpot list id) pair. Contacts are **always**
sourced live from the configured HubSpot list — there is no CSV paste,
no manual entry, and no Twilio-side contact list. The detail page
pulls the HubSpot list at request time and decorates each contact
with attempt count / last outcome / last call time / in-progress
flag, looked up from the ``calls`` table by phone number.

Writes this module performs:

* ``POST /campaigns/`` — create a campaign (name + list id)
* ``POST /campaigns/<id>/status`` — Start / Pause / Done

Live updates: the detail page polls
``GET /campaigns/<id>/contacts.json`` every few seconds and updates
the table in place. A spinner appears next to any contact whose
most-recent call placement has not yet received a terminal webhook
(``in_progress=True``); it disappears and is replaced by the outcome
as soon as ``/voice`` or ``/status`` records the outcome.

No authentication — same posture as ``call_handler.py``. Templates
are inline Jinja strings with autoescape enabled.

Run with::

    flask --app dashboard run --port 5001
"""

from __future__ import annotations

import threading

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    url_for,
)

import hubspot_client
import scheduler
import state

app = Flask(__name__)

# Ensure the SQLite schema (including the Phase 5 campaigns table)
# exists before any request is served.
state.init_db()


# --- Templates -------------------------------------------------------------
# Inline Jinja strings. Autoescape is on, so untrusted text (HubSpot
# names / phones, campaign names) cannot inject HTML.

_BASE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ page_title or 'Campaigns' }} — Voicemail</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
      href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  /* Phase 6 design tokens — Refined Minimal / Calm Operator
     (see docs/phase6-plan.md). Class names and template structure
     are unchanged to keep the JSON-poll JS and Jinja contracts
     untouched. */
  :root {
    /* Surfaces & text — light */
    --bg: #F8FAFC;
    --surface: #FFFFFF;
    --surface-2: #F1F5F9;
    --border: #E2E8F0;
    --border-strong: #CBD5E1;
    --text: #0F172A;
    --text-muted: #64748B;
    /* Accent */
    --primary: #2563EB;
    --primary-hover: #1D4ED8;
    --primary-soft: #EFF4FF;
    /* Semantic */
    --success: #059669;
    --success-soft: #ECFDF5;
    --warning: #B45309;
    --warning-soft: #FFFBEB;
    --danger:  #DC2626;
    --danger-soft:  #FEF2F2;
    --neutral: #475569;
    --neutral-soft: #F1F5F9;
    /* Effects */
    --focus-ring: 0 0 0 3px rgba(37, 99, 235, 0.35);
    --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.04),
                 0 1px 3px rgba(15, 23, 42, 0.06);
    --shadow-md: 0 4px 12px rgba(15, 23, 42, 0.06),
                 0 2px 4px rgba(15, 23, 42, 0.04);
    /* Radius scale: 4 / 8 / 12 */
    --radius-sm: 4px;
    --radius: 8px;
    --radius-lg: 12px;
    --radius-pill: 999px;
    /* Spacing scale: 4 / 8 / 12 / 16 / 24 / 32 / 48 */
    --space-1: 4px;
    --space-2: 8px;
    --space-3: 12px;
    --space-4: 16px;
    --space-5: 24px;
    --space-6: 32px;
    --space-7: 48px;
    /* Typography */
    --font-ui: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
               Roboto, "Helvetica Neue", Arial, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular,
                 "SF Mono", Menlo, Consolas, monospace;
    /* Motion */
    --transition: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0B1220;
      --surface: #111827;
      --surface-2: #1F2937;
      --border: #1F2937;
      --border-strong: #374151;
      --text: #E5E7EB;
      --text-muted: #94A3B8;
      --primary: #60A5FA;
      --primary-hover: #3B82F6;
      --primary-soft: rgba(96, 165, 250, 0.12);
      --success: #34D399;
      --success-soft: rgba(52, 211, 153, 0.12);
      --warning: #FBBF24;
      --warning-soft: rgba(251, 191, 36, 0.12);
      --danger:  #F87171;
      --danger-soft:  rgba(248, 113, 113, 0.12);
      --neutral: #94A3B8;
      --neutral-soft: rgba(148, 163, 184, 0.12);
      --focus-ring: 0 0 0 3px rgba(96, 165, 250, 0.45);
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
      --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.45);
    }
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  a { color: var(--primary); text-decoration: none;
      transition: color var(--transition); }
  a:hover { text-decoration: underline; }

  /* Visible keyboard focus ring on every interactive element. */
  :focus { outline: none; }
  a:focus-visible,
  button:focus-visible,
  input:focus-visible,
  select:focus-visible,
  textarea:focus-visible,
  [tabindex]:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
    border-radius: var(--radius-sm);
  }

  /* Screen-reader-only utility for table captions etc. */
  .sr-only {
    position: absolute;
    width: 1px; height: 1px;
    padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0);
    white-space: nowrap; border: 0;
  }

  code { font-family: var(--font-mono);
         font-size: 0.9em;
         background: var(--surface-2);
         padding: 0.1em 0.4em;
         border-radius: var(--radius-sm);
         border: 1px solid var(--border); }

  /* Top app bar */
  .appbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    box-shadow: var(--shadow-sm);
  }
  .appbar-inner {
    max-width: 1100px;
    margin: 0 auto;
    padding: 0.9rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-weight: 600;
    font-size: 1rem;
    color: var(--text);
  }
  .brand-mark {
    width: 28px; height: 28px;
    border-radius: 7px;
    background: linear-gradient(135deg, #2f6feb 0%, #1e3a8a 100%);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-weight: 700;
    font-size: 13px;
    box-shadow: var(--shadow-sm);
  }
  .breadcrumb {
    color: var(--text-muted);
    font-size: 0.9rem;
    margin-left: 0.5rem;
  }
  .breadcrumb a { color: var(--text-muted); }
  .breadcrumb a:hover { color: var(--primary); }
  .breadcrumb-sep { margin: 0 0.4rem; color: var(--border-strong); }

  /* Page shell */
  .container {
    max-width: 1100px;
    margin: 1.75rem auto;
    padding: 0 1.5rem;
  }
  .page-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }
  .page-title {
    margin: 0 0 0.25rem 0;
    font-size: 1.5rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .page-subtitle {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.95rem;
  }

  /* Cards */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
    margin-bottom: 1.25rem;
  }
  .card-header {
    padding: 1rem 1.25rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .card-title {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
  }
  .card-body { padding: 1.25rem; }
  .card-body.tight { padding: 0; }

  /* Tables */
  table { border-collapse: collapse; width: 100%; }
  thead th {
    background: var(--surface-2);
    color: var(--text-muted);
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.6rem 1.25rem;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  tbody td {
    padding: 0.75rem 1.25rem;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr.in-progress { background: var(--primary-soft); }
  .cell-phone { font-family: var(--font-mono);
                font-size: 0.92rem; }
  .cell-name { font-weight: 500; }
  .cell-muted { color: var(--text-muted); font-size: 0.9rem; }
  .num { font-variant-numeric: tabular-nums; }

  /* Status pills */
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    line-height: 1.6;
    border: 1px solid transparent;
  }
  .pill .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: currentColor;
  }
  .pill-active { color: var(--success);
                 background: var(--success-soft);
                 border-color: color-mix(in srgb, var(--success) 25%, transparent); }
  .pill-paused { color: var(--warning);
                 background: var(--warning-soft);
                 border-color: color-mix(in srgb, var(--warning) 25%, transparent); }
  .pill-done   { color: var(--neutral);
                 background: var(--neutral-soft);
                 border-color: var(--border-strong); }

  /* Outcome pills */
  .out-vm   { color: var(--success);
              background: var(--success-soft);
              border-color: color-mix(in srgb, var(--success) 22%, transparent); }
  .out-hum  { color: var(--primary);
              background: var(--primary-soft);
              border-color: color-mix(in srgb, var(--primary) 25%, transparent); }
  .out-no   { color: var(--warning);
              background: var(--warning-soft);
              border-color: color-mix(in srgb, var(--warning) 22%, transparent); }
  .out-bus  { color: var(--warning);
              background: var(--warning-soft);
              border-color: color-mix(in srgb, var(--warning) 22%, transparent); }
  .out-fail { color: var(--danger);
              background: var(--danger-soft);
              border-color: color-mix(in srgb, var(--danger) 22%, transparent); }
  .out-none { color: var(--text-muted); font-size: 0.85rem; }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.45rem 0.9rem;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--text);
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
    font-weight: 500;
    cursor: pointer;
    transition: background var(--transition), border-color var(--transition),
                box-shadow var(--transition), color var(--transition);
    text-decoration: none;
  }
  .btn:hover:not(:disabled) {
    background: var(--surface-2);
    border-color: var(--text-muted);
    text-decoration: none;
  }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-primary {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--primary-hover);
    border-color: var(--primary-hover);
  }
  .btn-success {
    background: var(--success);
    color: #fff;
    border-color: var(--success);
  }
  .btn-success:hover:not(:disabled) {
    filter: brightness(0.92);
  }
  .btn-warning {
    background: var(--surface);
    color: var(--warning);
    border-color: color-mix(in srgb, var(--warning) 45%, transparent);
  }
  .btn-danger {
    background: var(--surface);
    color: var(--neutral);
    border-color: var(--border-strong);
  }
  .btn-group { display: inline-flex; gap: 0.5rem; flex-wrap: wrap; }
  form.inline { display: inline; }

  /* Forms */
  .field { margin-bottom: 0.85rem; }
  .field label {
    display: block;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 0.3rem;
    font-size: 0.88rem;
  }
  .field .hint {
    display: block;
    color: var(--text-muted);
    font-size: 0.82rem;
    margin-top: 0.2rem;
  }
  input[type=text] {
    width: 100%;
    max-width: 24rem;
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    background: var(--surface);
    font-size: 0.92rem;
    color: var(--text);
    transition: border-color 0.12s ease, box-shadow 0.12s ease;
  }
  input[type=text]:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: var(--focus-ring);
  }

  /* Flash + error banners */
  .banner {
    border-radius: var(--radius-sm);
    padding: 0.7rem 1rem;
    margin-bottom: 1rem;
    font-size: 0.92rem;
    border: 1px solid transparent;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .banner-info { background: var(--primary-soft);
                 color: var(--primary-hover);
                 border-color: color-mix(in srgb, var(--primary) 30%, transparent); }
  .banner-error { background: var(--danger-soft);
                  color: var(--danger);
                  border-color: color-mix(in srgb, var(--danger) 30%, transparent); }

  /* Empty state */
  .empty {
    padding: 2.5rem 1.5rem;
    text-align: center;
    color: var(--text-muted);
  }
  .empty-title {
    color: var(--text);
    font-weight: 600;
    margin-bottom: 0.25rem;
  }

  /* Live spinner */
  .spinner {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid var(--primary-soft);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: -2px;
    margin-right: 0.4rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Pulse for in-progress row label */
  .live-label {
    display: inline-flex;
    align-items: center;
    color: var(--primary);
    font-size: 0.82rem;
    font-weight: 600;
  }

  /* Connection indicator (footer of detail page) */
  .live-indicator {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--text-muted);
    font-size: 0.82rem;
  }
  .live-indicator .live-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 60%, transparent);
    animation: pulse 1.8s ease-out infinite;
  }
  .live-indicator.stale .live-dot {
    background: var(--text-muted);
    animation: none;
    box-shadow: none;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 60%, transparent); }
    70%  { box-shadow: 0 0 0 8px color-mix(in srgb, var(--success) 0%, transparent); }
    100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 0%, transparent); }
  }

  /* Campaign metrics dashboard */
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.75rem;
  }
  .metric-tile {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.85rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .metric-tile .metric-label {
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .metric-tile .metric-value {
    font-size: 1.6rem;
    font-weight: 600;
    line-height: 1.1;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .metric-tile .metric-sub {
    color: var(--text-muted);
    font-size: 0.78rem;
  }
  .metric-tile.accent-primary .metric-value { color: var(--primary); }
  .metric-tile.accent-success .metric-value { color: var(--success); }
  .metric-tile.accent-warning .metric-value { color: var(--warning); }
  .metric-tile.accent-danger  .metric-value { color: var(--danger); }
  .metric-progress {
    margin-top: 0.4rem;
    height: 6px;
    width: 100%;
    background: var(--border);
    border-radius: 999px;
    overflow: hidden;
  }
  .metric-progress > span {
    display: block;
    height: 100%;
    background: var(--primary);
    transition: width 0.3s ease;
  }
  .outcomes-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.6rem;
    margin-top: 1rem;
  }
  .outcome-tile {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.6rem 0.8rem;
    background: var(--surface);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .outcome-tile .outcome-count {
    font-size: 1.1rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }

  .footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: var(--text-muted);
    font-size: 0.82rem;
    margin-top: 1.5rem;
    padding: 0 0.25rem;
  }

  /* Inline action icons (Lucide). Inherits currentColor from button. */
  .icon {
    width: 14px; height: 14px;
    flex-shrink: 0;
    stroke: currentColor;
    stroke-width: 2;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  /* Skeleton / loading affordance.
     The 2.5s JSON poll mutates row text nodes in place. To avoid a
     flicker, we (a) add a brief shimmer to in-progress rows so the
     "this cell is about to change" intent is visible, and (b) expose a
     reusable .skeleton utility for future placeholder rows. */
  tbody tr.in-progress td {
    position: relative;
    overflow: hidden;
  }
  tbody tr.in-progress td::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(
      90deg,
      transparent 0%,
      color-mix(in srgb, var(--primary) 6%, transparent) 50%,
      transparent 100%
    );
    transform: translateX(-100%);
    animation: row-shimmer 1.6s ease-in-out infinite;
    pointer-events: none;
  }
  @keyframes row-shimmer {
    100% { transform: translateX(100%); }
  }

  .skeleton {
    display: inline-block;
    min-width: 4rem;
    height: 0.9em;
    border-radius: var(--radius-sm);
    background: linear-gradient(
      90deg,
      var(--surface-2) 0%,
      var(--border) 50%,
      var(--surface-2) 100%
    );
    background-size: 200% 100%;
    animation: skeleton-shimmer 1.4s ease-in-out infinite;
    color: transparent;
  }
  @keyframes skeleton-shimmer {
    0%   { background-position: 100% 0; }
    100% { background-position: -100% 0; }
  }

  /* Respect prefers-reduced-motion: kill all decorative motion;
     keep transitions short but disable infinite/keyframe animations. */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.001ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.001ms !important;
      scroll-behavior: auto !important;
    }
    .spinner {
      border-top-color: var(--primary);
      animation: none;
    }
    .live-indicator .live-dot { animation: none; box-shadow: none; }
    tbody tr.in-progress td::after { display: none; }
  }
</style>
</head>
<body>
<header class="appbar">
  <div class="appbar-inner">
    <a class="brand" href="{{ url_for('list_campaigns') }}">
      <span class="brand-mark">V</span>
      <span>Voicemail Console</span>
    </a>
  </div>
</header>
<main class="container">
"""

_BASE_FOOT = """
</main>
</body>
</html>
"""


# Helper macros expressed as Jinja, embedded in both templates so they
# can render outcome pills consistently on the server (initial render)
# and on the client (live updates use a JS port — see _CONTACTS_JS).
_OUTCOME_PILL_MACRO = """
{% macro outcome_pill(outcome) -%}
  {%- if outcome == 'Voicemail Left' -%}
    <span class="pill out-vm"><span class="dot"></span>Voicemail Left</span>
  {%- elif outcome == 'Human Answered' -%}
    <span class="pill out-hum"><span class="dot"></span>Human Answered</span>
  {%- elif outcome == 'No Answer' -%}
    <span class="pill out-no"><span class="dot"></span>No Answer</span>
  {%- elif outcome == 'Busy' -%}
    <span class="pill out-bus"><span class="dot"></span>Busy</span>
  {%- elif outcome == 'Failed' -%}
    <span class="pill out-fail"><span class="dot"></span>Failed</span>
  {%- else -%}
    <span class="out-none">—</span>
  {%- endif -%}
{%- endmacro %}
"""


_LIST_TMPL = _BASE_HEAD + _OUTCOME_PILL_MACRO + """
<div class="page-header">
  <div>
    <h1 class="page-title">Campaigns</h1>
    <p class="page-subtitle">Outbound voicemail campaigns sourced from HubSpot lists.</p>
  </div>
</div>

{% if flash %}<div class="banner banner-info">{{ flash }}</div>{% endif %}

<section class="card">
  <header class="card-header">
    <h2 class="card-title">All campaigns</h2>
    <span class="cell-muted">{{ campaigns|length }} total</span>
  </header>
  <div class="card-body tight">
    {% if campaigns %}
    <table>
      <caption class="sr-only">All voicemail campaigns</caption>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">HubSpot list</th>
          <th scope="col">Status</th>
          <th scope="col">Created</th>
        </tr>
      </thead>
      <tbody>
      {% for c in campaigns %}
        <tr>
          <td class="cell-name">
            <a href="{{ url_for('campaign_detail', campaign_id=c.id) }}">{{ c.name }}</a>
          </td>
          <td><code>{{ c.hubspot_list_id }}</code></td>
          <td>
            <span class="pill pill-{{ c.status }}">
              <span class="dot"></span>{{ c.status }}
            </span>
          </td>
          <td class="cell-muted">{{ c.created_at }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty">
      <div class="empty-title">No campaigns yet</div>
      <div>Create one below to start dialling a HubSpot list.</div>
    </div>
    {% endif %}
  </div>
</section>

<section class="card">
  <header class="card-header">
    <h2 class="card-title">New campaign</h2>
  </header>
  <div class="card-body">
    <form method="post" action="{{ url_for('create_campaign') }}">
      <div class="field">
        <label for="name">Name</label>
        <input id="name" type="text" name="name" required
               placeholder="e.g. October re-engagement">
      </div>
      <div class="field">
        <label for="hubspot_list_id">HubSpot list ID</label>
        <input id="hubspot_list_id" type="text" name="hubspot_list_id"
               required placeholder="e.g. 1234">
        <span class="hint">The numeric ID of the HubSpot contact list to dial.</span>
      </div>
      <button type="submit" class="btn btn-primary">Create campaign</button>
    </form>
  </div>
</section>
""" + _BASE_FOOT


_DETAIL_TMPL = _BASE_HEAD + _OUTCOME_PILL_MACRO + """
<nav class="breadcrumb">
  <a href="{{ url_for('list_campaigns') }}">Campaigns</a>
  <span class="breadcrumb-sep">/</span>
  <span>{{ campaign.name }}</span>
</nav>

<div class="page-header">
  <div>
    <h1 class="page-title">{{ campaign.name }}</h1>
    <p class="page-subtitle">
      HubSpot list <code>{{ campaign.hubspot_list_id }}</code>
      · Created {{ campaign.created_at }}
    </p>
  </div>
  <div>
    <span id="campaign-status" class="pill pill-{{ campaign.status }}">
      <span class="dot"></span>{{ campaign.status }}
    </span>
  </div>
</div>

{% if flash %}<div class="banner banner-info">{{ flash }}</div>{% endif %}
{% if error %}<div class="banner banner-error">{{ error }}</div>{% endif %}

<section class="card">
  <header class="card-header">
    <h2 class="card-title">Campaign controls</h2>
  </header>
  <div class="card-body">
    <div class="btn-group">
      <form class="inline" method="post"
            action="{{ url_for('set_status', campaign_id=campaign.id) }}">
        <input type="hidden" name="status" value="active">
        <button type="submit" class="btn btn-success"
                {% if campaign.status == 'active' %}disabled{% endif %}>
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          Start
        </button>
      </form>
      <form class="inline" method="post"
            action="{{ url_for('set_status', campaign_id=campaign.id) }}">
        <input type="hidden" name="status" value="paused">
        <button type="submit" class="btn btn-warning"
                {% if campaign.status == 'paused' %}disabled{% endif %}>
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
          Pause
        </button>
      </form>
      <form class="inline" method="post"
            action="{{ url_for('set_status', campaign_id=campaign.id) }}">
        <input type="hidden" name="status" value="done">
        <button type="submit" class="btn btn-danger"
                {% if campaign.status == 'done' %}disabled{% endif %}>
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>
          Done
        </button>
      </form>
      <form class="inline" method="post"
            action="{{ url_for('run_campaign', campaign_id=campaign.id) }}">
        <button type="submit" class="btn btn-primary"
                {% if campaign.status != 'active' %}disabled{% endif %}>
          Run All
        </button>
      </form>
    </div>
    <p class="hint" style="margin-top:0.6rem;margin-bottom:0">
      Run All dials every contact in this campaign once. Campaign must be Active.
    </p>
  </div>
</section>

<section class="card" id="metrics-card">
  <header class="card-header">
    <h2 class="card-title">Campaign metrics</h2>
    <span class="cell-muted">Live overview</span>
  </header>
  <div class="card-body">
    <div class="metrics-grid">
      <div class="metric-tile">
        <span class="metric-label">Total contacts</span>
        <span class="metric-value" data-metric="total">{{ metrics.total }}</span>
        <span class="metric-sub">From HubSpot list</span>
      </div>
      <div class="metric-tile accent-success">
        <span class="metric-label">Called</span>
        <span class="metric-value" data-metric="called">{{ metrics.called }}</span>
        <span class="metric-sub">
          <span data-metric="reach_pct">{{ metrics.reach_pct }}</span>% reach
        </span>
        <div class="metric-progress" aria-hidden="true">
          <span data-metric-bar="reach"
                style="width: {{ metrics.reach_pct }}%"></span>
        </div>
      </div>
      <div class="metric-tile">
        <span class="metric-label">Not called yet</span>
        <span class="metric-value" data-metric="not_called">{{ metrics.not_called }}</span>
        <span class="metric-sub">Remaining</span>
      </div>
      <div class="metric-tile accent-primary" aria-live="polite" aria-atomic="true">
        <span class="metric-label">In progress</span>
        <span class="metric-value" data-metric="in_progress">{{ metrics.in_progress }}</span>
        <span class="metric-sub">Dialing right now</span>
      </div>
      <div class="metric-tile">
        <span class="metric-label">Total attempts</span>
        <span class="metric-value" data-metric="total_attempts">{{ metrics.total_attempts }}</span>
        <span class="metric-sub">Calls placed</span>
      </div>
    </div>

    <div class="outcomes-grid">
      {% for name in metrics.outcome_order %}
      <div class="outcome-tile">
        {{ outcome_pill(name) }}
        <span class="outcome-count"
              data-outcome="{{ name }}">{{ metrics.outcomes[name] }}</span>
      </div>
      {% endfor %}
    </div>
  </div>
</section>

<section class="card">
  <header class="card-header">
    <h2 class="card-title">Contacts</h2>
    <span id="contact-count" class="cell-muted">{{ contacts|length }} from HubSpot</span>
  </header>
  <div class="card-body tight">
    {% if contacts %}
    <table id="contacts-table"
           data-feed="{{ url_for('contacts_json', campaign_id=campaign.id) }}">
      <caption class="sr-only">Contacts in this campaign with live call status</caption>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Phone</th>
          <th scope="col" class="num">Attempts</th>
          <th scope="col">Last outcome</th>
          <th scope="col">Last call</th>
        </tr>
      </thead>
      <tbody id="contacts-body">
      {% for c in contacts %}
        <tr data-phone="{{ c.phone or '' }}"
            class="{% if c.in_progress %}in-progress{% endif %}">
          <td class="cell-name">{{ c.name }}</td>
          <td class="cell-phone">
            <span class="js-live">
              {% if c.in_progress -%}
                <span class="spinner" aria-label="Dialing"></span>
                <span class="live-label">Dialing…</span>
              {%- endif %}
            </span>
            <span class="js-phone">{{ c.phone or '' }}</span>
            {% if c.phone_raw and not c.phone -%}
              <span class="cell-muted"> ({{ c.phone_raw }} — unparsed)</span>
            {%- endif %}
          </td>
          <td class="js-attempts num">{{ c.attempt_count }}</td>
          <td class="js-outcome">{{ outcome_pill(c.last_outcome) }}</td>
          <td class="js-last-at cell-muted">{{ c.last_call_at or '' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% elif not error %}
    <div class="empty">
      <div class="empty-title">No contacts in this HubSpot list</div>
      <div>Add contacts to the list in HubSpot, then refresh.</div>
    </div>
    {% endif %}
  </div>
</section>

<div class="footer">
  <span id="live-indicator" class="live-indicator">
    <span class="live-dot"></span><span class="live-text">Live</span>
  </span>
  <span class="cell-muted">Polling every <span id="poll-secs">…</span>s</span>
</div>

<script>
(function () {
  var table = document.getElementById("contacts-table");
  if (!table) return;
  var feed = table.dataset.feed;
  var indicator = document.getElementById("live-indicator");
  var liveText = indicator.querySelector(".live-text");
  var POLL_MS = 2500;

  var OUTCOMES = [
    "Voicemail Left", "Human Answered", "No Answer", "Busy", "Failed"
  ];

  function computeMetrics(contacts) {
    var m = {
      total: contacts.length,
      called: 0,
      not_called: 0,
      in_progress: 0,
      total_attempts: 0,
      reach_pct: 0,
      outcomes: {}
    };
    OUTCOMES.forEach(function (o) { m.outcomes[o] = 0; });
    contacts.forEach(function (c) {
      var attempts = c.attempt_count || 0;
      m.total_attempts += attempts;
      if (attempts > 0) m.called += 1;
      if (c.in_progress) m.in_progress += 1;
      if (c.last_outcome && m.outcomes.hasOwnProperty(c.last_outcome)) {
        m.outcomes[c.last_outcome] += 1;
      }
    });
    m.not_called = Math.max(m.total - m.called, 0);
    m.reach_pct = m.total ? Math.round((m.called / m.total) * 100) : 0;
    return m;
  }

  function setMetricText(name, value) {
    document
      .querySelectorAll('[data-metric="' + name + '"]')
      .forEach(function (el) { el.textContent = String(value); });
  }

  function updateMetrics(contacts) {
    var card = document.getElementById("metrics-card");
    if (!card) return;
    var m = computeMetrics(contacts);
    setMetricText("total", m.total);
    setMetricText("called", m.called);
    setMetricText("not_called", m.not_called);
    setMetricText("in_progress", m.in_progress);
    setMetricText("total_attempts", m.total_attempts);
    setMetricText("reach_pct", m.reach_pct);
    var bar = card.querySelector('[data-metric-bar="reach"]');
    if (bar) bar.style.width = m.reach_pct + "%";
    OUTCOMES.forEach(function (o) {
      var el = card.querySelector('[data-outcome="' + o + '"]');
      if (el) el.textContent = String(m.outcomes[o]);
    });
  }

  function pillHtml(outcome) {
    if (!outcome) return '<span class="out-none">—</span>';
    var map = {
      "Voicemail Left": "out-vm",
      "Human Answered": "out-hum",
      "No Answer":      "out-no",
      "Busy":           "out-bus",
      "Failed":         "out-fail"
    };
    var cls = map[outcome];
    if (!cls) return '<span class="out-none">—</span>';
    var span = document.createElement("span");
    span.className = "pill " + cls;
    var dot = document.createElement("span");
    dot.className = "dot";
    span.appendChild(dot);
    span.appendChild(document.createTextNode(outcome));
    return span.outerHTML;
  }

  function updateRow(row, c) {
    // Spinner / "Dialing…" label.
    var live = row.querySelector(".js-live");
    if (live) {
      if (c.in_progress) {
        live.innerHTML = '<span class="spinner" aria-label="Dialing"></span>'
                       + '<span class="live-label">Dialing\u2026</span>';
        row.classList.add("in-progress");
      } else {
        live.innerHTML = "";
        row.classList.remove("in-progress");
      }
    }
    // Attempt count.
    var atts = row.querySelector(".js-attempts");
    if (atts) atts.textContent = String(c.attempt_count || 0);
    // Outcome pill.
    var out = row.querySelector(".js-outcome");
    if (out) out.innerHTML = pillHtml(c.last_outcome);
    // Last call timestamp.
    var ts = row.querySelector(".js-last-at");
    if (ts) ts.textContent = c.last_call_at || "";
  }

  function tick() {
    fetch(feed, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        indicator.classList.remove("stale");
        liveText.textContent = "Live";
        var contactsList = data.contacts || [];
        // Update each row by phone match. We don't add/remove rows;
        // the HubSpot list is a server-rendered snapshot.
        var byPhone = {};
        contactsList.forEach(function (c) {
          if (c.phone) byPhone[c.phone] = c;
        });
        document.querySelectorAll("#contacts-body tr").forEach(function (row) {
          var p = row.dataset.phone;
          if (p && byPhone[p]) updateRow(row, byPhone[p]);
        });
        // Refresh the metrics dashboard from the same payload.
        updateMetrics(contactsList);
        // Status pill (e.g. if user pauses in another tab).
        var statusPill = document.getElementById("campaign-status");
        if (statusPill && data.campaign && data.campaign.status) {
          var st = data.campaign.status;
          statusPill.className = "pill pill-" + st;
          statusPill.innerHTML = '<span class="dot"></span>' + st;
        }
      })
      .catch(function () {
        indicator.classList.add("stale");
        liveText.textContent = "Reconnecting\u2026";
      });
  }

  document.getElementById("poll-secs").textContent =
    (POLL_MS / 1000).toFixed(1);
  setInterval(tick, POLL_MS);
  // Run once shortly after load so freshly-placed calls appear quickly.
  setTimeout(tick, 400);
})();
</script>
""" + _BASE_FOOT


# --- Helpers ---------------------------------------------------------------

def _decorate_contacts(raw_contacts: list[dict]) -> list[dict]:
    """Normalise + decorate HubSpot contacts with local call stats.

    Each row in the output has:
    ``name``, ``phone`` (E.164 or ``None``), ``phone_raw``,
    ``attempt_count``, ``last_outcome``, ``last_call_at``, ``in_progress``.
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
            "in_progress": False,
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
                r["in_progress"] = s["in_progress"]
    return rows


# Outcomes we surface as their own tile in the metrics dashboard.
# Kept in render order so the UI stays stable.
_METRIC_OUTCOMES: tuple[str, ...] = (
    "Voicemail Left",
    "Human Answered",
    "No Answer",
    "Busy",
    "Failed",
)


def _compute_metrics(contacts: list[dict]) -> dict:
    """Derive campaign-level metrics from decorated contacts.

    Pure read-only aggregation over the rows already produced by
    :func:`_decorate_contacts` — no DB or HubSpot calls, no impact on
    existing logic. The detail template renders this server-side and
    the polling JS recomputes the same shape client-side from
    ``/contacts.json`` so the dashboard stays live.
    """
    total = len(contacts)
    called = 0
    in_progress = 0
    total_attempts = 0
    outcomes: dict[str, int] = {name: 0 for name in _METRIC_OUTCOMES}
    for c in contacts:
        attempts = int(c.get("attempt_count") or 0)
        total_attempts += attempts
        if attempts > 0:
            called += 1
        if c.get("in_progress"):
            in_progress += 1
        outcome = c.get("last_outcome")
        if outcome in outcomes:
            outcomes[outcome] += 1
    not_called = max(total - called, 0)
    reach_pct = round((called / total) * 100) if total else 0
    return {
        "total": total,
        "called": called,
        "not_called": not_called,
        "in_progress": in_progress,
        "total_attempts": total_attempts,
        "reach_pct": reach_pct,
        "outcomes": outcomes,
        # Ordered list so Jinja can iterate predictably.
        "outcome_order": list(_METRIC_OUTCOMES),
    }


# --- Routes ----------------------------------------------------------------

@app.get("/")
def index() -> Response:
    return redirect(url_for("list_campaigns"), code=302)


@app.get("/campaigns/")
def list_campaigns() -> str:
    campaigns = state.list_campaigns()
    flash = request.args.get("flash") or ""
    return render_template_string(
        _LIST_TMPL,
        campaigns=campaigns,
        flash=flash,
        page_title="Campaigns",
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
        metrics=_compute_metrics(contacts),
        flash=flash,
        error=error,
        page_title=campaign["name"],
    )


@app.get("/campaigns/<int:campaign_id>/contacts.json")
def contacts_json(campaign_id: int):
    """JSON feed used by the detail page for live updates.

    Returns the same decorated contacts as the HTML view plus the
    current campaign status. Phones that fail to normalise are
    included with ``phone: null`` so the client can ignore them.
    """
    campaign = state.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    contacts: list[dict] = []
    error = None
    try:
        raw = hubspot_client.list_contacts(campaign["hubspot_list_id"])
        contacts = _decorate_contacts(raw)
    except Exception as exc:  # noqa: BLE001 - JSON endpoint must not 500
        print(
            f"[dashboard] ERROR loading HubSpot list "
            f"{campaign['hubspot_list_id']!r} (json): {exc}",
            flush=True,
        )
        error = "hubspot_unavailable"
    return jsonify({
        "campaign": {
            "id": campaign["id"],
            "name": campaign["name"],
            "status": campaign["status"],
            "hubspot_list_id": campaign["hubspot_list_id"],
        },
        "contacts": contacts,
        "error": error,
    })


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


@app.post("/campaigns/<int:campaign_id>/run")
def run_campaign(campaign_id: int) -> Response:
    campaign = state.get_campaign(campaign_id)
    if not campaign:
        abort(404)
    if campaign["status"] != "active":
        return redirect(
            url_for(
                "campaign_detail",
                campaign_id=campaign_id,
                flash="Campaign must be Active before running.",
            ),
            code=303,
        )

    def _run() -> None:
        try:
            result = scheduler.run_once()
            print(
                f"[dashboard] Run All finished: "
                f"{result['dialed']} dialed, {result['skipped']} skipped",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboard] Run All error: {exc}", flush=True)

    threading.Thread(target=_run, daemon=True).start()
    return redirect(
        url_for(
            "campaign_detail",
            campaign_id=campaign_id,
            flash="Dialing started — watch the contacts table for live updates.",
        ),
        code=303,
    )


@app.get("/healthz")
def healthz() -> tuple[str, int]:
    return ("ok", 200)


if __name__ == "__main__":
    # Convenience for `python dashboard.py`; production should use
    # `flask --app dashboard run` or a proper WSGI server.
    app.run(host="0.0.0.0", port=5001)
