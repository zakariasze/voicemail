# Phase 3 — HubSpot integration

> Scope per `README.md → Build Phases → Phase 3`:
> * `hubspot_client.py` — pull contact list, log call activity, update
>   `Last Call Attempt` and `Last Call Outcome` properties.
> * Script to create the two custom HubSpot properties on first run.
> * Wire outcome from Phase 2 into HubSpot logging.
> * Verification: run against one real HubSpot contact, confirm
>   activity appears on their timeline.

## What this phase adds (and only this phase)

| File | Change |
|---|---|
| `requirements.txt` | Add `requests` (one new dep — see decision §1). |
| `.env.example`, `config.py` | New env var `HUBSPOT_LIST_ID` + `config.hubspot_api_key()` / `config.hubspot_list_id()` accessors. |
| `hubspot_client.py` | **New.** Thin v3 REST wrapper. Public surface: `ensure_custom_properties()`, `list_contacts()`, `get_contact(id)`, `log_call(contact_id, outcome, when, duration_seconds=None)`, plus a CLI (`setup` / `list` / `call-one`). |
| `state.py` | Add columns `hubspot_contact_id` and `hubspot_logged_at`. Additive, idempotent migration via `ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check. New helper `record_call_placed(call_sid, to_number, hubspot_contact_id)`. |
| `twilio_client.py` | `place_call(to_number, hubspot_contact_id=None)` now records a placement row in SQLite at call-creation time so the webhooks can look the contact up later by `CallSid`. |
| `call_handler.py` | `/status` now pushes to HubSpot once the call is finalized (after `/voice` has set the outcome). Idempotent via `hubspot_logged_at`. `/voice` is unchanged — it does not touch HubSpot. |

Nothing else changes. No scheduler, no dashboard.

## Decisions and why

### 1. `requests` over stdlib `urllib`
The HubSpot v3 API needs JSON bodies, bearer-token auth, query strings,
and clear HTTP error handling. Stdlib `urllib.request` does all of
that but verbosely. `requests` is the de-facto standard, tiny, has no
known CVEs at 2.32.3, and the README's "simple, maintainable Python —
no over-engineering" principle favors it. Total deps stay at four
(flask, twilio, python-dotenv, requests).

### 2. `requests` over the official `hubspot-api-client`
The official SDK is huge (transitive deps) and we use ~5 endpoints.
Direct REST keeps the surface tiny, the code grep-able, and avoids an
SDK version-pinning headache.

### 3. v3 endpoints used (and only these)

| Purpose | Endpoint |
|---|---|
| Create custom property | `POST /crm/v3/properties/contacts` |
| Check custom property exists | `GET /crm/v3/properties/contacts/{name}` |
| Get one contact's phone | `GET /crm/v3/objects/contacts/{id}?properties=phone,firstname,lastname` |
| Get list memberships (contact IDs) | `GET /crm/v3/lists/{listId}/memberships/join-order` |
| Batch read contacts | `POST /crm/v3/objects/contacts/batch/read` |
| Patch contact properties | `PATCH /crm/v3/objects/contacts/{id}` |
| Create call engagement | `POST /crm/v3/objects/calls` |

All require the `oauth` scope `crm.objects.contacts.write`,
`crm.objects.contacts.read`, `crm.schemas.contacts.write`, and
`crm.objects.calls.write`. The user is using a private-app token
(`HUBSPOT_API_KEY` env var sent as `Authorization: Bearer …`).

### 4. Custom properties

Both live on the `contacts` object.

* `last_call_attempt` — type `datetime`, group `contactinformation`,
  label "Last Call Attempt". Written as ISO-8601 UTC.
* `last_call_outcome` — type `enumeration`, field type `select`,
  group `contactinformation`, label "Last Call Outcome", options
  exactly: `Voicemail Left`, `Human Answered`, `No Answer`, `Busy`,
  `Failed` (using the labels themselves as both label and internal
  value so the value matches the README outcomes verbatim).

`ensure_custom_properties()` is idempotent: it `GET`s each property
first and only `POST`s if a 404 comes back. A 409 ("already exists")
from `POST` is also tolerated, so racing two setup runs is safe.

### 5. Call engagement payload

```jsonc
POST /crm/v3/objects/calls
{
  "properties": {
    "hs_timestamp": "<call-start ISO 8601>",
    "hs_call_title": "Outbound voicemail drop",
    "hs_call_body":  "Outcome: <Voicemail Left | …>",
    "hs_call_direction": "OUTBOUND",
    "hs_call_status": "COMPLETED",   // always — even No Answer is a completed attempt from our side
    "hs_call_duration": "<ms or omitted>"
  },
  "associations": [{
    "to": { "id": "<contact_id>" },
    "types": [{
      "associationCategory": "HUBSPOT_DEFINED",
      "associationTypeId": 194        // call → contact (HubSpot-defined)
    }]
  }]
}
```

Association type ID `194` is HubSpot's well-known call→contact ID.

### 6. Where the HubSpot write happens
Single write point: **`/status` on the final webhook of a call.**

* `/voice` only writes to SQLite (the AMD outcome). Reasons:
  - At `/voice` the call is still active and we don't yet know the
    duration.
  - `/voice` must return TwiML fast — slow HubSpot calls would stall
    audio playback.
* `/status` fires once at call end regardless of outcome. By that
  point SQLite has the canonical `outcome`. We push to HubSpot only
  if:
  1. SQLite row has both `outcome` AND `hubspot_contact_id` set.
  2. `hubspot_logged_at` is still `NULL`.
* `hubspot_logged_at` is set atomically only after a successful
  HubSpot write, so Twilio retries of `/status` are safe.
* If HubSpot is down, we log the error and return 204 to Twilio
  anyway (no point making Twilio retry — a Phase 4/5 reconcile job
  can replay un-logged rows).

### 7. Schema migration strategy
SQLite. Two new columns, both nullable, no rewrites. Migration is
`ALTER TABLE calls ADD COLUMN ...` issued only if `PRAGMA
table_info(calls)` doesn't already list the column. Idempotent and
keeps existing Phase 2 rows intact.

### 8. List API choice
v3 lists API (`/crm/v3/lists/{id}/memberships/join-order`) over the
deprecated `contacts/v1/lists/{id}/contacts/all`. The user provided
`HUBSPOT_LIST_ID=5` — a numeric ID works for both. If the v3 endpoint
returns 404 for this particular list (older lists tool), the error
message tells the user how to remediate (rebuild the list in the new
lists UI). We don't auto-fall-back, to keep the code paths simple.

### 9. Phone-number normalization
HubSpot stores phones in user-entered form ("(555) 123-4567"). Twilio
wants strict E.164. We strip everything except `+` and digits, and
if there's no leading `+` we prepend `+1` (current scope is US dental
practices per the README). Phase 4 may revisit this for multi-country.

### 10. Outbound flow used for verification

```
python hubspot_client.py setup          # creates the 2 properties
python hubspot_client.py list           # prints contacts in list 5
python hubspot_client.py call-one <id>  # places one call → end-to-end
```

`call-one` calls `get_contact(id)` → `twilio_client.place_call(phone,
hubspot_contact_id=id)`. Twilio webhooks run as in Phase 2; `/status`
then writes to HubSpot.

## Out of scope (later phases)
- Iterating the whole list and scheduling calls in time windows → Phase 4.
- Two-attempt tracking → Phase 4.
- Dashboard → Phase 5.

## Manual verification

```bash
# 1. ensure properties exist
python hubspot_client.py setup
# Expect:
#   [hubspot] property last_call_attempt: created
#   [hubspot] property last_call_outcome: created  (or already exists)

# 2. confirm we can read the list
python hubspot_client.py list
# Expect: a JSON-like printout of contacts (id, firstname, lastname, phone)

# 3. place one call against a real contact
python hubspot_client.py call-one <CONTACT_ID>
# (Use your own number on a contact for the test.)
# Expect on your phone: voicemail plays as in Phase 2.
# Expect in Flask terminal:
#   [voice]  CallSid=... AnsweredBy=... -> Voicemail Left
#   [status] CallSid=... CallStatus=completed
#   [hubspot] logged call for contact <id>: Voicemail Left

# 4. open the contact in HubSpot
# Expect:
#   - A "Call" activity on the timeline titled "Outbound voicemail drop"
#     with body "Outcome: Voicemail Left"
#   - Contact properties "Last Call Attempt" and "Last Call Outcome" populated
```
