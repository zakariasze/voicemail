"""Minimal HubSpot CRM v3 client.

We only need a handful of endpoints — pulling a contact list, fetching
one contact's phone, ensuring two custom properties exist, and logging
a call engagement plus property update after each dial. Using the
official ``hubspot-api-client`` would add a large transitive footprint
for very little gain, so this module talks v3 REST directly via
``requests``.

Public surface
--------------
* :func:`ensure_custom_properties` — idempotently create the two
  Phase 3 custom contact properties (``last_call_attempt`` and
  ``last_call_outcome``).
* :func:`list_contacts` — return all contacts in the configured list
  with the fields we care about.
* :func:`get_contact` — fetch one contact by id.
* :func:`log_call` — write a single call activity to a contact's
  timeline and patch the two custom properties.

A CLI is provided for the Phase 3 verification step:

    python hubspot_client.py setup
    python hubspot_client.py list
    python hubspot_client.py call-one <contact_id>

The CLI is the intended way to verify Phase 3 end-to-end against one
real contact.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests

import config

API_BASE = "https://api.hubapi.com"

# HubSpot-defined association type id for call → contact.
ASSOC_TYPE_CALL_TO_CONTACT = 194

# README outcome labels also serve as internal enum values, so they
# round-trip cleanly without an extra label-to-value mapping.
_OUTCOME_OPTIONS = [
    "Voicemail Left",
    "Human Answered",
    "Transferred",
    "No Answer",
    "Busy",
    "Failed",
]

PROP_LAST_CALL_ATTEMPT = "last_call_attempt"
PROP_LAST_CALL_OUTCOME = "last_call_outcome"


class HubSpotError(RuntimeError):
    """Raised when a HubSpot API call returns a non-success status."""

    def __init__(self, status: int, body: str, *, method: str, path: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


# --- HTTP transport --------------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.hubspot_api_key()}",
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    allow_status: tuple[int, ...] = (),
) -> tuple[int, dict[str, Any] | None]:
    """Low-level HubSpot call.

    Returns ``(status, parsed_json_or_None)``. Raises :class:`HubSpotError`
    for any non-2xx status except those listed in ``allow_status`` (used
    e.g. to tolerate 404 from a property-exists check, or 409 from a
    racing property creation).
    """
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method,
        url,
        headers=_headers(),
        params=params,
        data=json.dumps(json_body) if json_body is not None else None,
        timeout=30,
    )
    if resp.status_code >= 400 and resp.status_code not in allow_status:
        raise HubSpotError(resp.status_code, resp.text, method=method, path=path)
    parsed: dict[str, Any] | None
    if resp.content:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
    else:
        parsed = None
    return resp.status_code, parsed


# --- Custom properties -----------------------------------------------------

def _property_exists(name: str) -> bool:
    status, _ = _request(
        "GET",
        f"/crm/v3/properties/contacts/{name}",
        allow_status=(404,),
    )
    return status == 200


def ensure_custom_properties() -> dict[str, str]:
    """Create the two Phase 3 contact properties if they don't exist.

    Idempotent: existing properties are left untouched. Returns a
    ``{property_name: "created" | "exists"}`` map so the CLI can print
    something useful.
    """
    results: dict[str, str] = {}

    if _property_exists(PROP_LAST_CALL_ATTEMPT):
        results[PROP_LAST_CALL_ATTEMPT] = "exists"
    else:
        _create_property(
            name=PROP_LAST_CALL_ATTEMPT,
            label="Last Call Attempt",
            type_="datetime",
            field_type="date",
        )
        results[PROP_LAST_CALL_ATTEMPT] = "created"

    if _property_exists(PROP_LAST_CALL_OUTCOME):
        results[PROP_LAST_CALL_OUTCOME] = "exists"
    else:
        _create_property(
            name=PROP_LAST_CALL_OUTCOME,
            label="Last Call Outcome",
            type_="enumeration",
            field_type="select",
            options=[
                {"label": opt, "value": opt, "displayOrder": i, "hidden": False}
                for i, opt in enumerate(_OUTCOME_OPTIONS)
            ],
        )
        results[PROP_LAST_CALL_OUTCOME] = "created"

    return results


def _create_property(
    *,
    name: str,
    label: str,
    type_: str,
    field_type: str,
    options: list[dict[str, Any]] | None = None,
) -> None:
    body: dict[str, Any] = {
        "name": name,
        "label": label,
        "type": type_,
        "fieldType": field_type,
        "groupName": "contactinformation",
        "description": "Set automatically by the voicemail automation agent.",
    }
    if options is not None:
        body["options"] = options
    # 409 = already exists (another process raced us); treat as success.
    _request(
        "POST",
        "/crm/v3/properties/contacts",
        json_body=body,
        allow_status=(409,),
    )


# --- Contacts --------------------------------------------------------------

_CONTACT_PROPS = ["firstname", "lastname", "phone"]


def get_contact(contact_id: str) -> dict[str, Any]:
    """Return ``{"id": ..., "firstname": ..., "lastname": ..., "phone": ...}``.

    Raises :class:`HubSpotError` if the contact does not exist.
    """
    _, data = _request(
        "GET",
        f"/crm/v3/objects/contacts/{contact_id}",
        params={"properties": ",".join(_CONTACT_PROPS)},
    )
    return _flatten_contact(data or {})


def list_contacts(list_id: str | None = None) -> list[dict[str, Any]]:
    """Return all contacts in the configured list.

    Uses the v3 lists membership endpoint to enumerate contact IDs,
    then batch-reads the contacts to fetch the properties we care
    about. ``list_id`` defaults to ``config.hubspot_list_id()``.
    """
    list_id = list_id or config.hubspot_list_id()

    ids: list[str] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if after:
            params["after"] = after
        _, data = _request(
            "GET",
            f"/crm/v3/lists/{list_id}/memberships/join-order",
            params=params,
        )
        data = data or {}
        for row in data.get("results", []):
            rid = row.get("recordId")
            if rid:
                ids.append(str(rid))
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break

    if not ids:
        return []

    contacts: list[dict[str, Any]] = []
    # Batch read 100 at a time.
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        _, data = _request(
            "POST",
            "/crm/v3/objects/contacts/batch/read",
            json_body={
                "properties": _CONTACT_PROPS,
                "inputs": [{"id": cid} for cid in chunk],
            },
        )
        for row in (data or {}).get("results", []):
            contacts.append(_flatten_contact(row))
    return contacts


def _flatten_contact(row: dict[str, Any]) -> dict[str, Any]:
    props = row.get("properties") or {}
    return {
        "id": str(row.get("id", "")),
        "firstname": props.get("firstname"),
        "lastname": props.get("lastname"),
        "phone": props.get("phone"),
    }


# --- Call logging ----------------------------------------------------------

def log_call(
    contact_id: str,
    *,
    outcome: str,
    when: datetime | None = None,
    duration_seconds: float | None = None,
    notes: str | None = None,
) -> str:
    """Write a Call activity to the contact's timeline and update props.

    Returns the created engagement's id (useful for logs / debugging).
    """
    if not contact_id:
        raise ValueError("contact_id is required")
    if outcome not in _OUTCOME_OPTIONS:
        raise ValueError(f"unknown outcome: {outcome!r}")

    when = when or datetime.now(timezone.utc)
    ts = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    body_lines = [f"Outcome: {outcome}"]
    if notes:
        body_lines.append(notes)

    call_props: dict[str, Any] = {
        "hs_timestamp": ts,
        "hs_call_title": "Outbound voicemail drop",
        "hs_call_body": "\n".join(body_lines),
        "hs_call_direction": "OUTBOUND",
        "hs_call_status": "COMPLETED",
    }
    if duration_seconds is not None:
        call_props["hs_call_duration"] = str(int(duration_seconds * 1000))

    _, created = _request(
        "POST",
        "/crm/v3/objects/calls",
        json_body={
            "properties": call_props,
            "associations": [
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": ASSOC_TYPE_CALL_TO_CONTACT,
                        }
                    ],
                }
            ],
        },
    )

    # Update the two custom contact properties.
    _request(
        "PATCH",
        f"/crm/v3/objects/contacts/{contact_id}",
        json_body={
            "properties": {
                PROP_LAST_CALL_ATTEMPT: ts,
                PROP_LAST_CALL_OUTCOME: outcome,
            }
        },
    )

    return str((created or {}).get("id", ""))


# --- Phone normalization ---------------------------------------------------

_PHONE_CLEAN = re.compile(r"[^0-9+]")


def normalize_phone(raw: str | None) -> str | None:
    """Return ``raw`` in strict Twilio-compatible E.164 form.

    HubSpot stores phones however a sales rep typed them. Twilio
    requires ``+<country><digits>``. We strip everything that isn't a
    digit or a leading ``+`` and prepend ``+1`` if no country code is
    present (current scope is US dental practices per the README).
    Returns ``None`` if the input has no digits.
    """
    if not raw:
        return None
    cleaned = _PHONE_CLEAN.sub("", raw)
    # Drop any stray '+' that isn't at position 0.
    if "+" in cleaned[1:]:
        cleaned = cleaned[0] + cleaned[1:].replace("+", "")
    if not re.search(r"\d", cleaned):
        return None
    if cleaned.startswith("+"):
        return cleaned
    if len(cleaned) == 10:
        return "+1" + cleaned
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return "+" + cleaned
    # Unknown shape — return as-is with a leading +, let Twilio reject it.
    return "+" + cleaned


# --- CLI -------------------------------------------------------------------

def _cli_setup() -> int:
    results = ensure_custom_properties()
    for name, status_ in results.items():
        print(f"[hubspot] property {name}: {status_}")
    return 0


def _cli_list() -> int:
    contacts = list_contacts()
    print(f"[hubspot] {len(contacts)} contact(s) in list "
          f"{config.hubspot_list_id()}:")
    for c in contacts:
        print(json.dumps(c))
    return 0


def _cli_call_one(contact_id: str) -> int:
    # Import here to avoid pulling Twilio into the import path when
    # only running `setup` or `list`.
    import twilio_client

    contact = get_contact(contact_id)
    phone = normalize_phone(contact.get("phone"))
    if not phone:
        print(
            f"[hubspot] contact {contact_id} has no usable phone "
            f"(raw={contact.get('phone')!r})",
            file=sys.stderr,
        )
        return 2
    print(
        f"[hubspot] contact {contact_id} "
        f"({contact.get('firstname')} {contact.get('lastname')}) -> {phone}"
    )
    twilio_client.place_call(phone, hubspot_contact_id=contact_id)
    print(
        "[hubspot] call placed. Watch the Flask terminal; the\n"
        "          [status] handler will push the outcome to HubSpot\n"
        "          once Twilio reports the call has ended."
    )
    return 0


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage:\n"
            "  python hubspot_client.py setup\n"
            "  python hubspot_client.py list\n"
            "  python hubspot_client.py call-one <contact_id>",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    if cmd == "setup":
        return _cli_setup()
    if cmd == "list":
        return _cli_list()
    if cmd == "call-one":
        if len(argv) != 3:
            print("Usage: python hubspot_client.py call-one <contact_id>",
                  file=sys.stderr)
            return 2
        return _cli_call_one(argv[2])
    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
