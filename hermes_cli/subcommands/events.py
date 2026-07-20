"""CLI for publishing an external event into the bound gateway session."""

from __future__ import annotations

import json
import os
import sys

from gateway.external_events import (
    MAX_PAYLOAD_BYTES,
    TOKEN_ENV_VAR,
    ExternalEventError,
    publish_event,
)


def publish_from_stdin() -> None:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(MAX_PAYLOAD_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        print("Error: event exceeds the 64 KiB input limit.", file=sys.stderr)
        raise SystemExit(2)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("Error: stdin must contain one valid JSON object.", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(payload, dict):
        print("Error: stdin must contain one JSON object.", file=sys.stderr)
        raise SystemExit(2)
    try:
        publish_event(payload, token_file=os.environ.get(TOKEN_ENV_VAR, ""))
    except ExternalEventError:
        print("Error: unable to publish the event.", file=sys.stderr)
        raise SystemExit(1)
    except OSError:
        print("Error: unable to write the event.", file=sys.stderr)
        raise SystemExit(1)
    print("Event queued.")


def build_events_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "events",
        help="Publish events for the current gateway-bound session",
    )
    actions = parser.add_subparsers(dest="events_action", required=True)
    publish = actions.add_parser(
        "publish",
        help="Queue one JSON object read from stdin",
        description=(
            "Queue one JSON object (maximum 64 KiB) for the exact gateway session "
            "bound to this subprocess. Routing arguments are not accepted."
        ),
    )
    publish.set_defaults(func=lambda _args: publish_from_stdin())
