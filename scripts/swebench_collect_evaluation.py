#!/usr/bin/env python3
"""Normalize SWE-bench harness output into run-local evaluation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def infer_agent(run_dir: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    run_config = read_json(run_dir / "run.json") if (run_dir / "run.json").exists() else {}
    agents = run_config.get("agents") or []
    if isinstance(agents, list) and len(agents) == 1:
        return str(agents[0])
    agent_dirs = sorted(path.name for path in (run_dir / "agents").glob("*") if path.is_dir())
    if len(agent_dirs) == 1:
        return agent_dirs[0]
    raise SystemExit("could not infer agent; pass --agent")


def result_for(instance_id: str, report: dict[str, Any]) -> str:
    buckets = [
        ("error", set(report.get("error_ids") or [])),
        ("empty_patch", set(report.get("empty_patch_ids") or [])),
        ("resolved", set(report.get("resolved_ids") or [])),
        ("unresolved", set(report.get("unresolved_ids") or [])),
        ("incomplete", set(report.get("incomplete_ids") or [])),
        ("submitted_unknown", set(report.get("submitted_ids") or [])),
    ]
    for status, ids in buckets:
        if instance_id in ids:
            return status
    return "not_submitted"


def task_order(run_dir: Path, report: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for task in read_jsonl(run_dir / "tasks.jsonl"):
        instance_id = str(task.get("instance_id") or "")
        if instance_id and instance_id not in seen:
            ids.append(instance_id)
            seen.add(instance_id)
    for field in ("submitted_ids", "resolved_ids", "unresolved_ids", "empty_patch_ids", "error_ids", "incomplete_ids"):
        for raw_instance_id in report.get(field) or []:
            instance_id = str(raw_instance_id)
            if instance_id not in seen:
                ids.append(instance_id)
                seen.add(instance_id)
    return ids


def metadata_for(run_dir: Path, agent: str, instance_id: str) -> dict[str, Any]:
    path = run_dir / "agents" / agent / instance_id / "metadata.json"
    if not path.exists():
        return {}
    return read_json(path)


def build_rows(run_dir: Path, agent: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for instance_id in task_order(run_dir, report):
        metadata = metadata_for(run_dir, agent, instance_id)
        patch_path = run_dir / "agents" / agent / instance_id / "final.patch"
        patch_bytes = metadata.get("patch_bytes")
        if patch_bytes is None and patch_path.exists():
            patch_bytes = patch_path.stat().st_size
        rows.append(
            {
                "instance_id": instance_id,
                "agent": agent,
                "agent_status": metadata.get("status"),
                "exit_code": metadata.get("exit_code"),
                "duration_seconds": metadata.get("duration_seconds"),
                "patch_bytes": patch_bytes,
                "swebench_result": result_for(instance_id, report),
            }
        )
    return rows


def build_summary(run_dir: Path, agent: str, report: dict[str, Any]) -> dict[str, Any]:
    total = int(report.get("total_instances") or 0)
    submitted = int(report.get("submitted_instances") or 0)
    resolved = int(report.get("resolved_instances") or 0)
    empty = int(report.get("empty_patch_instances") or 0)
    non_empty = max(submitted - empty, 0)
    return {
        "run_id": run_dir.name,
        "agent": agent,
        "total": total,
        "submitted": submitted,
        "completed_tests": int(report.get("completed_instances") or 0),
        "resolved": resolved,
        "unresolved": int(report.get("unresolved_instances") or 0),
        "empty_patch": empty,
        "errors": int(report.get("error_instances") or 0),
        "resolved_rate_total": (resolved / total) if total else None,
        "resolved_rate_non_empty": (resolved / non_empty) if non_empty else None,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["instance_id", "agent", "agent_status", "exit_code", "duration_seconds", "patch_bytes", "swebench_result"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--report", type=Path, help="SWE-bench harness report JSON; defaults to run_dir/evaluation/report.json")
    parser.add_argument("--agent", help="Agent name when it cannot be inferred from the run")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.exists():
        raise SystemExit(f"missing run directory: {run_dir}")
    eval_dir = run_dir / "evaluation"
    report_path = args.report or eval_dir / "report.json"
    if not report_path.exists():
        raise SystemExit(f"missing SWE-bench report JSON: {report_path}")

    eval_dir.mkdir(parents=True, exist_ok=True)
    canonical_report = eval_dir / "report.json"
    if report_path.resolve() != canonical_report.resolve():
        shutil.copy2(report_path, canonical_report)
    report = read_json(canonical_report)
    agent = infer_agent(run_dir, args.agent)
    rows = build_rows(run_dir, agent, report)
    summary = build_summary(run_dir, agent, report)

    write_json(eval_dir / "summary.json", summary)
    write_jsonl(eval_dir / "per_instance.jsonl", rows)
    write_csv(eval_dir / "per_instance.csv", rows)
    print(json.dumps({"evaluation_dir": str(eval_dir), **summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
