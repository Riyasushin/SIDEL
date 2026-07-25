#!/usr/bin/env python3
"""Write periodic, machine-readable progress snapshots for an L4 experiment."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CLAUDE_REQUEST_TIMEOUT = re.compile(r"request timed out", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--started-at", required=True, help="ISO-8601 UTC start time")
    parser.add_argument("--interval-seconds", type=float, default=4 * 60 * 60)
    parser.add_argument("--watch", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_started_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def context_count(run_root: Path) -> int:
    # A resume run may intentionally register only its pending contexts.  The
    # experiment directory remains the stable denominator for the whole run.
    manifest_count = sum(1 for _ in (run_root / "experiments").glob("*/manifest.json"))
    if manifest_count:
        return manifest_count
    registry = read_json(run_root / "router" / "effective_config.json") or {}
    contexts = registry.get("contexts")
    return len(contexts) if isinstance(contexts, dict) else 0


def snapshot(run_root: Path, started_at: datetime) -> dict[str, Any]:
    experiments = run_root / "experiments"
    outcomes: Counter[str] = Counter()
    completed_contexts: set[str] = set()
    claude_request_timeouts = 0

    for exit_path in experiments.glob("*/container/exit.json"):
        payload = read_json(exit_path) or {}
        status = str(payload.get("status") or "unknown")
        context_id = exit_path.parents[1].name
        outcomes[status] += 1
        completed_contexts.add(context_id)
        if status == "agent_crashed":
            logs = (exit_path.parent / "stdout.log", exit_path.parent / "stderr.log")
            if any(path.exists() and CLAUDE_REQUEST_TIMEOUT.search(path.read_text(encoding="utf-8", errors="replace")) for path in logs):
                claude_request_timeouts += 1

    l4_statuses: Counter[str] = Counter()
    l4_successes = 0
    for injection_path in experiments.glob("*/router/traces/*/injection.json"):
        payload = read_json(injection_path) or {}
        dynamic_l4 = payload.get("dynamic_l4")
        if not isinstance(dynamic_l4, dict):
            continue
        status = str(dynamic_l4.get("status") or "unknown")
        l4_statuses[status] += 1
        if status == "optimized":
            l4_successes += 1

    total = context_count(run_root)
    completed = len(completed_contexts)
    now = datetime.now(UTC)
    elapsed_seconds = max(0.0, (now - started_at).total_seconds())
    rate_per_second = completed / elapsed_seconds if elapsed_seconds > 0 else 0.0
    remaining_contexts = max(0, total - completed)
    eta_seconds = remaining_contexts / rate_per_second if rate_per_second > 0 else None
    outer_timeouts = outcomes["agent_timeout"]

    return {
        "timestamp": now.isoformat(),
        "run_id": run_root.name,
        "started_at": started_at.isoformat(),
        "total_contexts": total,
        "completed_contexts": completed,
        "progress_percent": round((100 * completed / total), 3) if total else 0.0,
        "remaining_contexts": remaining_contexts,
        "outcome_counts": dict(sorted(outcomes.items())),
        "timeout_counts": {
            "agent_timeout": outer_timeouts,
            "claude_request_timeout": claude_request_timeouts,
            "total": outer_timeouts + claude_request_timeouts,
        },
        "l4_rewrite": {
            "successful": l4_successes,
            "not_successfully_rewritten": sum(count for status, count in l4_statuses.items() if status != "optimized"),
            "status_counts": dict(sorted(l4_statuses.items())),
            "note": "Strict L4 writes injection.json only after a validated rewrite; an in-flight or client-aborted attempt without injection.json is not counted as an L4 result.",
        },
        "elapsed_seconds": round(elapsed_seconds, 3),
        "observed_contexts_per_hour": round(rate_per_second * 3600, 3),
        "estimated_remaining_seconds": round(eta_seconds, 3) if eta_seconds is not None else None,
        "estimate_basis": "completed contexts divided by wall-clock time since this restart; early estimates are volatile.",
    }


def write_snapshot(run_root: Path, started_at: datetime) -> Path:
    payload = snapshot(run_root, started_at)
    report_dir = run_root / "progress_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (report_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return path


def main() -> int:
    args = parse_args()
    started_at = parse_started_at(args.started_at)
    while True:
        write_snapshot(args.run_root.resolve(), started_at)
        if not args.watch:
            return 0
        time.sleep(max(1.0, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
