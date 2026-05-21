"""Entry point for the voicemail-drop agent.

What this does, in order:

1. Ensure the SQLite schema exists (``state.init_db``).
2. Ensure the two HubSpot custom contact properties exist
   (``hubspot_client.ensure_custom_properties``).
3. Run a single scheduler pass (``scheduler.run_once``), or loop if
   ``--loop`` is passed.

This is the "scheduler bootstrap" referenced in the README. It does
**not** start the Flask webhook — ``call_handler.py`` runs as a
separate process so that webhook latency is decoupled from the
scheduler tick. In dev:

    # Terminal A: webhook
    flask --app call_handler run --port 5000

    # Terminal B: scheduler
    python main.py

In production this script is intended to be invoked by cron once per
window (one pass per tick).
"""

from __future__ import annotations

import sys

import hubspot_client
import scheduler
import state


def _main(argv: list[str]) -> int:
    # Pass any args through to the scheduler CLI parser so callers can
    # do `python main.py --dry-run` / `--interval 10` / `--loop`.
    state.init_db()
    try:
        hubspot_client.ensure_custom_properties()
    except Exception as exc:  # noqa: BLE001 - bootstrap, fail loud but keep going
        print(
            f"[main] WARNING: could not ensure HubSpot custom properties: {exc}",
            flush=True,
        )
    return scheduler._main(["scheduler.py", *argv[1:]])


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
