# Phase 5 — Dashboard (campaigns)

> Scope per `README.md → Build Phases → Phase 5`:
> * `dashboard.py` — minimal Flask page; date, total dialed, outcome
>   counts, contact list with outcomes, human-follow-up flag.
> * Verification: dashboard reflects SQLite state correctly.

## User modification accepted

The user replaced the read-only dashboard with a small **campaigns**
feature. The contacts being dialed always come from a HubSpot list —
each campaign just selects which list to use. There is no CSV paste,
no manual entry, and no Twilio-side contact list.

Direct quote, after an early misread:

> "I want the list of contacts being pulled from hubspot list only.
>  Those are the numbers that are being dialed not manual entry or
>  pulling from the twilio list of contacts"

And the original 7 clarifications from the chat (still in force):

> 1. `/campaigns/{id}` detail page: table with one row per contact —
>    name, phone, attempt count, last outcome, last call time.
>    Page-level Start / Pause / Done.
> 2. No additional routes, no JSON API, no exports, no recording
>    playback.
> 3. Scheduler: dial from the active campaign's HubSpot list. Keep
>    the existing HubSpot-list source as a **fallback** (no active
>    campaign → fall back to the env-default list). Track attempt
>    count via the existing `calls` table. **No max attempts.**
> 4. Status transitions: manual only. Scheduler never auto-transitions.
> 5. Outcomes recorded locally in SQLite, and (because contacts are
>    real HubSpot contacts) also pushed to HubSpot by the existing
>    `/status` webhook — no special case needed.
> 6. Templates: inline Jinja strings in `dashboard.py`. No `templates/`.
> 7. Auth: none.

So this phase delivers:

* A `campaigns` table in the existing SQLite file (no
  `campaign_contacts` — contacts live in HubSpot, not here).
* `dashboard.py` — a small Flask app with three pages and one
  action endpoint, rendered from inline Jinja strings.
* A surgical change to `scheduler.py`: pick the source HubSpot list
  based on whether there is an active campaign.

## Files added / modified

| File | Change |
|---|---|
| `dashboard.py` | **NEW.** Flask app, inline Jinja, no auth. |
| `state.py` | **Modified.** Added `campaigns` table + helpers. No existing function is changed. |
| `scheduler.py` | **Modified.** Source selection: active campaign's `hubspot_list_id` first, env-default list as fallback. Existing behaviour is preserved when no campaign is active. |
| `docs/phase5-plan.md` | This file. |

`requirements.txt`, `main.py`, `call_handler.py`, `twilio_client.py`,
`hubspot_client.py`, `config.py`, `.env.example` are untouched.

## Decisions

### 1. Campaign = (name, hubspot_list_id)
A campaign is just two pieces of metadata: a human-readable name and
the HubSpot list it sources contacts from. Contacts are not snapshotted
into SQLite — the detail page and scheduler both pull live from
HubSpot every request / every pass. This keeps "what's in the list"
authoritative in HubSpot and avoids stale-snapshot bugs.

### 2. Campaign status values
Exactly the three values the user named: `active`, `paused`, `done`.
A `CHECK` constraint enforces this so a typo in a future caller fails
loudly. New campaigns start `paused` so creating a campaign does
not immediately start dialling.

### 3. Attempts and last outcome — JOIN on `to_number`
The detail page calls `hubspot_client.list_contacts(list_id)` to get
the contacts, normalises each phone, then calls
`state.phone_call_stats(phones)` which does a single `SELECT … FROM
calls WHERE to_number IN (…)` to get attempt count and most-recent
outcome / call time per phone. A phone with no rows gets
`attempt_count=0` and `None` for the other two fields.

### 4. No max attempts on campaigns
The user said "dial every contact in the active campaign every run".
The Phase 4 `MAX_ATTEMPTS` filter was already removed in commit
`70e9e0d`; both the campaign path and the fallback path now dial every
contact every pass.

### 5. Source selection in `scheduler.run_once`
At the top of `run_once`, `_pick_source()` returns either the active
campaign's HubSpot list contacts or the env-default list contacts,
plus a label for logging. The rest of `run_once` is unchanged —
`pending_contacts(…)` filtering, dialing with `hubspot_contact_id`
set, and the existing HubSpot logging in `/status` all work exactly
as before because campaign contacts are real HubSpot contacts.

### 6. Picking among multiple active campaigns
If more than one campaign is `active`, `state.get_active_campaign()`
returns the oldest by `created_at`. The dashboard never *requires*
exactly one active campaign — that's a soft single-user convention —
but the scheduler always has a deterministic choice. The user is
expected to manage status manually.

### 7. Inline Jinja, no `templates/`
The user asked for inline strings. They are kept short and use
Jinja's auto-escape (the default for `render_template_string` in
Flask) so HubSpot names and campaign names cannot inject HTML.

### 8. HubSpot fetch errors on the detail page
The detail page calls a remote HubSpot endpoint at request time, so
it can fail (network, 4xx, bad list id). The view catches any
exception and renders an inline error banner instead of crashing the
page. The Start / Pause / Done buttons still work in this state so
the user can pause / move on without the dashboard becoming wedged.

### 9. Flash messages
Flashes use Flask's `url_for(..., flash=...)` so the query string is
properly URL-encoded; they're rendered through autoescape so they
are XSS-safe even if a value comes from user input.

## Routes

| Method + path | Purpose |
|---|---|
| `GET  /` | Redirect to `/campaigns/`. |
| `GET  /campaigns/` | List campaigns (id, name, list id, status, created). Form to create a new campaign (name + HubSpot list id). |
| `POST /campaigns/` | Create a campaign. Redirect to the new campaign's detail page. |
| `GET  /campaigns/<id>` | Detail page: contacts table (live from HubSpot) + Start / Pause / Done buttons. |
| `POST /campaigns/<id>/status` | Set the campaign's status. Redirect back to detail. |
| `GET  /healthz` | Trivial liveness probe. |

No JSON API, no per-contact actions, no exports, no recording playback
— per item 2 of the user spec.

## Verification (manual)

1. Apply migrations: `python state.py` — self-test must print
   `state.py self-test PASS` (covers campaigns table, `hubspot_list_id`
   column, status enum, `get_active_campaign`, `phone_call_stats`).
2. Boot the dashboard: `flask --app dashboard run --port 5001`.
3. Open `http://localhost:5001/campaigns/`. Create a campaign named
   "smoke" with the HubSpot list id of a list that has 1–2 contacts in
   it. Confirm the detail page shows those HubSpot contacts with
   attempt count `0` and status `paused`.
4. Click **Start**. Confirm status flips to `active`.
5. Run `python scheduler.py --dry-run`. Confirm the log line
   `[scheduler] source: campaign <id> (HubSpot list <list_id>)` and
   that it lists the campaign's contacts, not the env-default list.
6. Click **Pause**. Run `python scheduler.py --dry-run` again.
   Confirm `[scheduler] source: HubSpot list (default)` and that it
   uses the env-configured `HUBSPOT_LIST_ID`.
7. Click **Done**. Confirm status flips to `done` and the dry-run
   again uses the env-default list.
