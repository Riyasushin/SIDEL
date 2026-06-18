#!/usr/bin/env python3
"""Build a replay manifest from router full trace artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router.replay import stable_hash


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def trace_root(path: Path) -> Path:
    if (path / "router-traces" / "conversations").exists():
        return path / "router-traces"
    if (path / "conversations").exists():
        return path
    raise SystemExit(f"no router trace conversations tree under {path}")


def build_manifest(root: Path) -> dict[str, Any]:
    conversations = root / "conversations"
    items: list[dict[str, Any]] = []
    for request_path in sorted(conversations.glob("*/*/*/request.agent.json")):
        turn_dir = request_path.parent
        rel = turn_dir.relative_to(conversations)
        agent, instance_id, turn_endpoint = rel.parts
        turn_s, _, endpoint = turn_endpoint.partition("-")
        response_path = turn_dir / "response.router.after.json"
        before_path = turn_dir / "response.router.before.json"
        item = {
            "agent": agent,
            "instance_id": instance_id,
            "turn_index": int(turn_s),
            "endpoint": endpoint,
            "turn_dir": str(turn_dir),
            "request_path": str(request_path),
            "request_hash": stable_hash(read_json(request_path)),
            "response_path": str(response_path) if response_path.exists() else "",
            "response_hash": stable_hash(read_json(response_path)) if response_path.exists() else "",
            "before_response_path": str(before_path) if before_path.exists() else "",
            "before_response_hash": stable_hash(read_json(before_path)) if before_path.exists() else "",
        }
        items.append(item)
    return {"trace_root": str(root), "count": len(items), "items": items}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_dir", type=Path, help="Run dir or router-traces dir")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    root = trace_root(args.trace_dir)
    manifest = build_manifest(root)
    output = args.output or root / "replay-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "count": manifest["count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
