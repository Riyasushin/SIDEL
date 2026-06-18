#!/usr/bin/env python3
"""Summarize a SWE-bench baseline run directory."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "timeout", "dry_run", "setup_failed", "interrupted"}


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def tail_lines(path: Path, count: int) -> list[str]:
    if count <= 0 or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-count:]


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.strip())


def read_jsonl_map(path: Path, key: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            value = row.get(key)
            if value is not None:
                rows[str(value)] = row
    return rows


def summarize(run_dir: Path, tail: int = 8) -> dict[str, Any]:
    agents_dir = run_dir / "agents"
    status_by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    patch_by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    swebench_by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    durations: dict[str, list[float]] = defaultdict(list)
    tasks_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    eval_summary = read_json(run_dir / "evaluation" / "summary.json")
    eval_rows = read_jsonl_map(run_dir / "evaluation" / "per_instance.jsonl", "instance_id")

    for metadata_path in sorted(agents_dir.glob("*/*/metadata.json")):
        metadata = read_json(metadata_path)
        agent = str(metadata.get("agent") or metadata_path.parents[1].name)
        status = str(metadata.get("status") or "unknown")
        status_by_agent[agent][status] += 1
        duration = metadata.get("duration_seconds")
        if isinstance(duration, (int, float)):
            durations[agent].append(float(duration))
        patch_path = metadata_path.parent / "final.patch"
        patch_bytes = int(metadata.get("patch_bytes") or (patch_path.stat().st_size if patch_path.exists() else 0))
        patch_by_agent[agent]["nonempty" if patch_bytes > 0 else "empty"] += 1
        instance_id = str(metadata.get("instance_id") or metadata_path.parent.name)
        eval_row = eval_rows.get(instance_id, {})
        swebench_result = eval_row.get("swebench_result")
        if swebench_result:
            swebench_by_agent[agent][str(swebench_result)] += 1
        tasks_by_agent[agent].append(
            {
                "instance_id": instance_id,
                "status": status,
                "exit_code": metadata.get("exit_code"),
                "duration_seconds": duration,
                "patch_bytes": patch_bytes,
                "swebench_result": swebench_result,
                "container": metadata.get("docker_agent_container"),
                "stderr_tail": tail_lines(metadata_path.parent / "stderr.log", tail),
            }
        )

    run_config = read_json(run_dir / "run.json")
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "run": run_config,
        "event_count": count_jsonl(run_dir / "events.jsonl"),
        "router_trace_count": count_jsonl(run_dir / "router-traces" / "router-kimi.jsonl"),
        "prediction_count": count_jsonl(run_dir / "predictions.jsonl"),
        "evaluation": eval_summary or None,
        "agents": {},
    }
    for agent in sorted(set(status_by_agent) | set(patch_by_agent) | set(tasks_by_agent)):
        values = durations.get(agent, [])
        task_rows = sorted(tasks_by_agent[agent], key=lambda row: row["instance_id"])
        summary["agents"][agent] = {
            "status": dict(status_by_agent[agent]),
            "patches": dict(patch_by_agent[agent]),
            "swebench": dict(swebench_by_agent[agent]),
            "avg_duration_seconds": round(sum(values) / len(values), 2) if values else None,
            "completed_or_terminal": sum(status_by_agent[agent][status] for status in TERMINAL_STATUSES),
            "tasks": task_rows,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--tail", type=int, default=8, help="stderr lines to include per task")
    args = parser.parse_args()
    summary = summarize(args.run_dir, tail=args.tail)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.write:
        (args.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
