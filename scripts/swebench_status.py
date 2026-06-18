#!/usr/bin/env python3
"""Show concise status for SWE-bench baseline runs."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ARTIFACT_ROOT = Path("artifacts/swebench-baseline")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def latest_run(root: Path) -> Path:
    runs = sorted((root / "runs").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        raise SystemExit(f"no runs under {root / 'runs'}")
    return runs[0]


def docker_ps() -> list[dict[str, str]]:
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].startswith("swebench-"):
            rows.append({"name": parts[0], "status": parts[1], "image": parts[2]})
    return rows


def status(run_dir: Path) -> dict[str, Any]:
    by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    running = []
    for metadata_path in sorted((run_dir / "agents").glob("*/*/metadata.json")):
        metadata = read_json(metadata_path)
        agent = str(metadata.get("agent") or metadata_path.parents[1].name)
        state = str(metadata.get("status") or "unknown")
        by_agent[agent][state] += 1
        if state == "running":
            running.append(
                {
                    "agent": agent,
                    "instance_id": metadata.get("instance_id"),
                    "started_at": metadata.get("started_at"),
                    "container": metadata.get("docker_agent_container"),
                }
            )
    return {
        "run_dir": str(run_dir),
        "run": read_json(run_dir / "run.json"),
        "agents": {agent: dict(counter) for agent, counter in sorted(by_agent.items())},
        "evaluation": read_json(run_dir / "evaluation" / "summary.json") or None,
        "running_tasks": running,
        "docker_containers": docker_ps(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", nargs="?", type=Path)
    parser.add_argument("--artifacts-root", type=Path, default=ARTIFACT_ROOT)
    args = parser.parse_args()
    run_dir = args.run_dir or latest_run(args.artifacts_root)
    print(json.dumps(status(run_dir), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
