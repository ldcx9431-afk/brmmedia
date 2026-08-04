#!/usr/bin/env python3
"""Call one Gradio API endpoint and wait for its handler SSE completion event.

For BRMMedia's asynchronous workflow submission endpoints, ``COMPLETE`` means
the task was accepted by the workspace queue, not that model inference has
finished.  Read the returned ``task_id`` and call ``task_status`` to track the
actual queued/running/completed/cancelled/failed lifecycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="Gradio API endpoint name without a leading slash")
    parser.add_argument(
        "data",
        help="JSON array of positional endpoint arguments; converted to Gradio v2 named fields",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()

    try:
        raw_data = Path(args.data[1:]).read_text(encoding="utf-8") if args.data.startswith("@") else args.data
        payload = {"data": json.loads(raw_data)}
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON data: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload["data"], list):
        print("ERROR: data must be a JSON array", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    try:
        with urlopen(f"{base}/gradio_api/info", timeout=30) as response:
            endpoint_info = json.load(response).get("named_endpoints", {}).get(f"/{args.endpoint}")
        if not endpoint_info:
            raise RuntimeError(f"unknown public Gradio endpoint: {args.endpoint}")
        parameters = endpoint_info.get("parameters", [])
        if len(parameters) != len(payload["data"]):
            raise RuntimeError(
                f"endpoint {args.endpoint} expects {len(parameters)} arguments, "
                f"received {len(payload['data'])}"
            )
        # Gradio 6's v2 protocol uses parameter names rather than the legacy
        # {"data": [...]} payload. Reading /info keeps this CLI compatible
        # with the actual running UI rather than duplicating its schema here.
        request_payload = {
            parameter["parameter_name"]: value
            for parameter, value in zip(parameters, payload["data"])
        }
    except (HTTPError, URLError, OSError, RuntimeError, KeyError) as exc:
        print(f"ERROR: could not inspect Gradio API: {exc}", file=sys.stderr)
        return 1

    request = Request(
        f"{base}/gradio_api/call/v2/{args.endpoint}",
        data=json.dumps(request_payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            event_id = json.load(response).get("event_id")
        if not event_id:
            raise RuntimeError("Gradio response did not contain event_id")

        print(f"EVENT_ID={event_id}", flush=True)
        stream_request = Request(
            f"{base}/gradio_api/call/v2/{args.endpoint}/{event_id}",
            headers={"Accept": "text/event-stream"},
        )
        event = None
        with urlopen(stream_request, timeout=args.timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                    if event == "complete":
                        print(f"COMPLETE={data}")
                        return 0
                    if event == "error":
                        print(f"ERROR={data}", file=sys.stderr)
                        return 1
    except (HTTPError, URLError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("ERROR: SSE stream ended without a completion event", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
