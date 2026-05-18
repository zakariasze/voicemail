# Phase 5 — Dashboard (campaigns)

> Scope per `README.md → Build Phases → Phase 5`:
> * `dashboard.py` — minimal Flask page; date, total dialed, outcome
>   counts, contact list with outcomes, human-follow-up flag.
> * Verification: dashboard reflects SQLite state correctly.

## User modification accepted

The user replaced the read-only dashboard with a small **campaigns**
feature on top of the existing call infrastructure. Direct quotes from
the chat:

> 1. `/campaigns/{id}` detail page: show a table with one row per
>    contact: name, phone, attempt count, last outcome, last call time.
>    Page-level actions: Start (active), Pause (paused), Done (done).
>    No per-contact actions.
> 2. Any routes beyond detail page: no additional routes, no JSON API,
>    no exports, no recording playback.
> 3. Scheduler integration: modify `scheduler.py` to dial from
>    `campaign_contacts` when any campaign has status `active`. Keep
>    the existing HubSpot-list source as a **fallback** (no active
>    campaign → fall back). Track attempt count by joining the
>    existing `calls` table on `to_number`. **No max attempts limit
>    — dial every contact in the active campaign every run.**
> 4. Status transitions: manual only. Scheduler never auto-transitions.
> 5. HubSpot for CSV phones: CSV campaigns skip HubSpot entirely.
>    Outcomes recorded locally in SQLite only.
> 6. Templates: inline Jinja strings in `dashboard.py`. No `templates/`.
> 7. Auth: none.

So this phase delivers:

* A `campaigns` and `campaign_contacts` table in the existing SQLite
  file, added via additive migrations in `state.py` so existing rows
  survive.
* `dashboard.py` — a small Flask app with three pages and one
  action endpoint, all rendered from inline Jinja strings.
* A surgical change to `scheduler.py`: if any campaign is `active`,
  dial that campaign's `campaign_contacts`; otherwise behave exactly
  as before (HubSpot list source).

## Files added / modified

| File | Change |
|---|---|
| `dashboard.py` | **NEW.** Flask app, inline Jinja, no auth. |
| `state.py` | **Modified.** Added `campaigns` / `campaign_contacts` tables (via the same `_MIGRATIONS` pattern used in Phase 3) and helper functions. No existing function is changed. |
| `scheduler.py` | **Modified.** Source selection: active campaigns first, HubSpot list as fallback. Existing behaviour is preserved when no campaign is active. |
| `docs/phase5-plan.md` | This file. |

`requirements.txt`, `main.py`, `call_handler.py`, `twilio_client.py`,
`hubspot_client.py`, `config.py`, `.env.example` are untouched.

## Decisions

### 1. Why store campaigns in the existing SQLite, not a new file
One file means a single backup, one migration story, and trivial joins
between `calls` and `campaign_contacts` for the "attempt count / last
outcome" columns on the detail page. `state.py` already follows an
additive-migration pattern (`_MIGRATIONS`); we extend it with two new
tables.

### 2. Campaign status values
Exactly the three values the user named: `active`, `paused`, `done`.
A `CHECK` constraint enforces this so a typo in a future caller fails
loudly. New campaigns start `paused` so that creating a campaign does
not immediately start dialling.

### 3. Attempts via JOIN on `to_number`
Existing `calls` rows already have `to_number` in E.164. Campaign
contacts also store `phone` in E.164 (normalised on insert via
`hubspot_client.normalize_phone`). A `LEFT JOIN calls ON
calls.to_number = campaign_contacts.phone` gives attempt count and the
most-recent outcome / `updated_at`. Caveat: if the same phone appears
in two campaigns the counts include calls placed by either; that's
acceptable for a single-user tool and avoids a more invasive
`calls.campaign_contact_id` column.

### 4. CSV campaigns skip HubSpot
`twilio_client.place_call` is called with `hubspot_contact_id=None`
for campaign contacts. The existing `/status` HubSpot push is already
gated on `hubspot_contact_id` being non-NULL (see
`call_handler._maybe_log_to_hubspot`), so this path naturally
short-circuits — no change needed there.

### 5. No max attempts on campaigns
The user explicitly said "dial every contact in the active campaign
every run". The Phase 4 `MAX_ATTEMPTS` filter only applied to the
HubSpot-list path and was already removed in commit `70e9e0d` ("remove
max attempts cap"). The campaign path never applies that filter to
begin with.

### 6. Source selection: active campaigns vs HubSpot list
At the top of `run_once`, query `campaigns` for any row with
`status='active'`. If found, dial those campaigns' contacts (in
`created_at` order across campaigns). If not, behave exactly as
before — `hubspot_client.list_contacts()` plus `pending_contacts(...)`.
This keeps the existing Phase 1–4 verification path intact.

### 7. CSV parsing
Accept pasted text in two shapes, one per line:

* `name,phone`
* `phone` alone

Lines are split with the stdlib `csv` module. Empty / whitespace-only
lines are skipped. Phones are run through `hubspot_client.normalize_phone`
on insert; rows whose phone fails to normalise are reported back to
the user on the create page (count of skipped lines) and **not**
inserted.

### 8. Inline Jinja, no `templates/`
The user asked for inline strings. They are kept short and use
Jinja's auto-escape (`Environment(autoescape=True)`) so CSV-pasted
names cannot inject HTML.

### 9. Actions are HTML form POSTs
`POST /campaigns/<id>/status` with form field `status=active|paused|done`,
plus a CSRF-free design (no auth, single-user tool, same posture as
`call_handler.py`). After a successful POST, redirect (303) to the
detail page so a refresh doesn't re-submit.

## Routes

| Method + path | Purpose |
|---|---|
| `GET  /` | Redirect to `/campaigns/`. |
| `GET  /campaigns/` | List campaigns (id, name, status, contact count). Form to create a new campaign (name + CSV textarea). |
| `POST /campaigns/` | Create a campaign and insert its contacts. Redirect to the new campaign's detail page. |
| `GET  /campaigns/<id>` | Detail page: contacts table + Start / Pause / Done buttons. |
| `POST /campaigns/<id>/status` | Set the campaign's status. Redirect back to detail. |
| `GET  /healthz` | Trivial liveness probe. |

No JSON API, no per-contact actions, no exports, no recording playback
— per item 2 of the user spec.

## Verification (manual)

1. Apply migrations: `python state.py` — self-test must still print
   `state.py self-test PASS` and now also verify the campaign tables.
2. Boot the dashboard: `flask --app dashboard run --port 5001`.
3. Open `http://localhost:5001/campaigns/`. Create a campaign named
   "smoke" with two pasted lines:
   ```
   Alice,555-111-2222
   555-333-4444
   ```
   Confirm the detail page shows two rows, both with attempt count
   `0`, status `paused`.
4. Click **Start**. Confirm status flips to `active`.
5. Run `python scheduler.py --dry-run`. Confirm it lists the two
   campaign phones (attempt 1) and *not* HubSpot contacts.
6. Click **Pause** on the campaign. Run `python scheduler.py --dry-run`
   again. Confirm it falls back to the HubSpot list path.
7. Click **Done**. Confirm status flips to `done` and the dry-run
   again uses the HubSpot list.
