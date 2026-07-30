#!/usr/bin/env python3
"""Validate that a ComfyUI instance exposes every node used by local workflows.

This intentionally does not submit jobs or allocate a model.  It is the safe
runtime preflight before the actual image, video, voice and music smoke tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen


DEFAULT_WORKFLOWS = Path(__file__).parent / "ubuntu-backend-deploy" / "workflows"


def required_nodes(workflow_path: Path) -> set[str]:
    with workflow_path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON value must be an object")

    nodes = {
        item["class_type"]
        for item in payload.values()
        if isinstance(item, dict) and isinstance(item.get("class_type"), str)
    }
    if not nodes:
        raise ValueError("no ComfyUI class_type entries found")
    return nodes


def node_exists(base_url: str, node_type: str, timeout: float) -> bool:
    endpoint = f"{base_url.rstrip('/')}/object_info/{quote(node_type, safe='')}"
    try:
        with urlopen(endpoint, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.load(response)
            return node_type in payload
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"{node_type}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"cannot reach ComfyUI at {base_url}: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8188", help="ComfyUI base URL")
    parser.add_argument("--workflows", type=Path, default=DEFAULT_WORKFLOWS)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    paths = sorted(args.workflows.glob("*.json"))
    if not paths:
        print(f"ERROR: no workflow JSON files in {args.workflows}", file=sys.stderr)
        return 2

    workflow_nodes: dict[Path, set[str]] = {}
    try:
        for path in paths:
            workflow_nodes[path] = required_nodes(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {path}: {exc}", file=sys.stderr)
        return 2

    availability: dict[str, bool] = {}
    try:
        for node_type in sorted(set().union(*workflow_nodes.values())):
            availability[node_type] = node_exists(args.url, node_type, args.timeout)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    missing_any = False
    for path, node_types in workflow_nodes.items():
        missing = sorted(node for node in node_types if not availability[node])
        if missing:
            missing_any = True
            print(f"MISSING  {path.name}: {', '.join(missing)}")
        else:
            print(f"READY    {path.name}: {len(node_types)} node types")

    print(f"SUMMARY workflows={len(paths)} node_types={len(availability)}")
    return 1 if missing_any else 0


if __name__ == "__main__":
    raise SystemExit(main())
