#!/usr/bin/env python3
"""Validate that ComfyUI exposes each workflow node and static enum asset.

This intentionally does not submit jobs or allocate a model.  In addition to
node class availability, it checks direct string values supplied to ComfyUI
combo inputs (for example model/checkpoint names) against the live choices.
It is the safe runtime preflight before image, video, voice and music smoke
tests.
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


def get_node_info(base_url: str, node_type: str, timeout: float) -> dict:
    endpoint = f"{base_url.rstrip('/')}/object_info/{quote(node_type, safe='')}"
    try:
        with urlopen(endpoint, timeout=timeout) as response:
            if response.status != 200:
                return {}
            payload = json.load(response)
            value = payload.get(node_type)
            return value if isinstance(value, dict) else {}
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        raise RuntimeError(f"{node_type}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"cannot reach ComfyUI at {base_url}: {exc.reason}") from exc


def enum_input_issues(workflow_path: Path, node_info: dict[str, dict]) -> list[str]:
    """Return invalid static combo choices without mistaking graph links for names."""
    with workflow_path.open(encoding="utf-8") as stream:
        workflow = json.load(stream)
    issues = []
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        node_type = node.get("class_type")
        inputs = node.get("inputs")
        definition = node_info.get(node_type, {})
        required = definition.get("input", {}).get("required", {})
        if not isinstance(inputs, dict) or not isinstance(required, dict):
            continue
        for input_name, value in inputs.items():
            # A graph edge is a list such as ["12", 0], while a selectable
            # asset/model name is a direct string.
            if not isinstance(value, str):
                continue
            spec = required.get(input_name)
            if not isinstance(spec, list) or not spec:
                continue
            choices = spec[0]
            if not isinstance(choices, list):
                continue
            options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            # Upload-capable fields enumerate files already present in ComfyUI's
            # input folder, but an API/UI submission may replace them.  A sample
            # filename in a checked-in workflow is therefore not a deployment
            # requirement; model selectors do not carry these upload flags.
            if any(key.endswith("_upload") and value is True for key, value in options.items()):
                continue
            if value not in choices:
                issues.append(f"{node_type}.{input_name}={value!r}")
    return issues


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

    definitions: dict[str, dict] = {}
    try:
        for node_type in sorted(set().union(*workflow_nodes.values())):
            definitions[node_type] = get_node_info(args.url, node_type, args.timeout)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    missing_any = False
    for path, node_types in workflow_nodes.items():
        missing_nodes = sorted(node for node in node_types if not definitions[node])
        try:
            missing_assets = enum_input_issues(path, definitions)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {path}: {exc}", file=sys.stderr)
            return 2
        if missing_nodes or missing_assets:
            missing_any = True
            if missing_nodes:
                print(f"MISSING_NODE   {path.name}: {', '.join(missing_nodes)}")
            if missing_assets:
                print(f"MISSING_ASSET  {path.name}: {', '.join(missing_assets)}")
        else:
            print(f"READY    {path.name}: {len(node_types)} node types")

    print(f"SUMMARY workflows={len(paths)} node_types={len(definitions)}")
    return 1 if missing_any else 0


if __name__ == "__main__":
    raise SystemExit(main())
