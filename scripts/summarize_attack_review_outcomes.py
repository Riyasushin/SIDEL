#!/usr/bin/env python3
"""Summarize direct and SWE-bench attack review outcomes."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DIRECT_ROOT = Path("artifacts/agent-review-gate")
SWEBENCH_RUN_ROOT = Path("artifacts/swebench-baseline/runs")
SUMMARY_ROOT = Path("artifacts/attack-review-summary")
TERMINAL_SWEBENCH_STATUSES = {"completed", "failed", "timeout", "dry_run", "setup_failed", "interrupted", "attack_observed_stopped"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_token(value: str) -> str:
    chars = [ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value]
    return "".join(chars).strip("-._") or "unknown"


def sample_from_row(row: dict[str, Any], run_dir: Path) -> str:
    injection = row.get("injection") if isinstance(row.get("injection"), dict) else {}
    sample_id = str(injection.get("sample_id") or "")
    if sample_id:
        return sample_id
    match = re.search(r"(malexec-\d+|[a-z]+-\d+)", run_dir.name)
    return match.group(1) if match else "unknown"


def turn_from_run(run_dir: Path) -> str:
    match = re.search(r"(?:swebench-turn|turn)([A-Za-z0-9_.-]+)-append-", run_dir.name)
    if match:
        return match.group(1)
    match = re.search(r"swebench-turn([A-Za-z0-9_.-]+)", run_dir.name)
    return match.group(1) if match else ""


def review_outcome(row: dict[str, Any]) -> str:
    review_decision = str(row.get("review_decision") or "")
    classification = str(row.get("classification") or "")
    review_observed = bool(row.get("review_observed"))
    execution_observed = bool(row.get("execution_observed"))
    if review_decision == "timeout" or classification == "auto_review_timeout":
        return "review_timeout"
    if review_decision == "denied" or classification == "denied":
        return "rejected"
    if review_decision == "allowed" or classification == "auto_review_allowed":
        return "allowed"
    if not review_observed:
        return "no_review_executed" if execution_observed else "no_review_no_execution"
    if execution_observed:
        return "allowed"
    if classification in {"timeout", "inconclusive"}:
        return "inconclusive"
    return classification or "unknown"


def iter_analysis_rows(prefixes: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prefix in prefixes:
        for run_dir in sorted(DIRECT_ROOT.glob(f"{prefix}-direct-*")):
            analysis = run_dir / "analysis" / "agent-attack-analysis.json"
            if not analysis.exists():
                continue
            data = read_json(analysis)
            for row in data.get("rows", []):
                rows.append(normalize_row(row, run_dir, "direct", prefix))
        patterns = [
            f"{prefix}-swebench-turn*-append-*",
            f"{prefix}-append-*",
        ]
        seen: set[Path] = set()
        for pattern in patterns:
            for run_dir in sorted(SWEBENCH_RUN_ROOT.glob(pattern)):
                if run_dir in seen:
                    continue
                seen.add(run_dir)
                analysis = run_dir / "analysis" / "agent-attack-analysis.json"
                if not analysis.exists():
                    continue
                data = read_json(analysis)
                for row in data.get("rows", []):
                    if str(row.get("status") or "") not in TERMINAL_SWEBENCH_STATUSES:
                        continue
                    rows.append(normalize_row(row, run_dir, "swebench", prefix))
    return rows


def normalize_row(row: dict[str, Any], run_dir: Path, experiment_type: str, source_prefix: str) -> dict[str, Any]:
    sample_id = sample_from_row(row, run_dir)
    normalized = {
        "source_prefix": source_prefix,
        "experiment_type": experiment_type,
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "turn": turn_from_run(run_dir) if experiment_type == "swebench" else "direct",
        "agent": row.get("agent", ""),
        "mode": row.get("normalized_mode") or row.get("mode", ""),
        "instance_id": row.get("instance_id", ""),
        "sample_id": sample_id,
        "classification": row.get("classification", ""),
        "status": row.get("status", ""),
        "marker_count": row.get("marker_count", ""),
        "injection_observed": row.get("injection_observed", False),
        "attack_tool_call_observed": row.get("attack_tool_call_observed", False),
        "attack_tool_result_observed": row.get("attack_tool_result_observed", False),
        "execution_observed": row.get("execution_observed", False),
        "replay_mode": row.get("replay_mode", ""),
        "replay_observed": row.get("replay_observed", False),
        "replay_request_matched": row.get("replay_request_matched", ""),
        "replay_source_response": row.get("replay_source_response", ""),
        "replay_request_hash": row.get("replay_request_hash", ""),
        "clean_request_hash": row.get("clean_request_hash", ""),
        "review_observed": row.get("review_observed", False),
        "review_decision": row.get("review_decision", ""),
        "review_trace_observed": row.get("review_trace_observed", False),
        "review_trace_decision": row.get("review_trace_decision", ""),
        "review_trace_count": row.get("review_trace_count", 0),
        "review_reason": row.get("review_reason", ""),
        "reason": row.get("reason", ""),
    }
    normalized["review_outcome"] = review_outcome(row)
    normalized["execution_outcome"] = "executed" if normalized["execution_observed"] else "not_executed"
    return normalized


def count_by(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, ...]] = Counter()
    for row in rows:
        counts[tuple(str(row.get(key, "")) for key in keys)] += 1
    output: list[dict[str, Any]] = []
    for values, count in sorted(counts.items()):
        item = {key: value for key, value in zip(keys, values, strict=True)}
        item["count"] = count
        output.append(item)
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id-prefix", action="append", required=True, help="Repeat or comma-separate to combine prefixes")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    prefixes = [part.strip() for item in args.run_id_prefix for part in item.split(",") if part.strip()]
    rows = iter_analysis_rows(prefixes)
    output_name = prefixes[0] if len(prefixes) == 1 else "combined-" + safe_token("__".join(prefixes))
    output_dir = args.output_dir or SUMMARY_ROOT / output_name
    output_dir.mkdir(parents=True, exist_ok=True)

    detail_fields = [
        "source_prefix",
        "experiment_type",
        "turn",
        "agent",
        "mode",
        "instance_id",
        "sample_id",
        "review_outcome",
        "execution_outcome",
        "classification",
        "status",
        "injection_observed",
        "attack_tool_call_observed",
        "attack_tool_result_observed",
        "execution_observed",
        "replay_mode",
        "replay_observed",
        "replay_request_matched",
        "replay_source_response",
        "replay_request_hash",
        "clean_request_hash",
        "review_observed",
        "review_decision",
        "review_reason",
        "reason",
        "run_id",
        "run_dir",
    ]
    write_csv(output_dir / "details.csv", rows, detail_fields)

    summary = {
        "run_id_prefixes": prefixes,
        "total_rows": len(rows),
        "counts_by_type_agent_outcome": count_by(rows, ("experiment_type", "agent", "review_outcome")),
        "counts_by_type_turn_agent_outcome": count_by(rows, ("experiment_type", "turn", "agent", "review_outcome")),
        "counts_by_type_agent_outcome_execution": count_by(rows, ("experiment_type", "agent", "review_outcome", "execution_outcome")),
        "counts_by_sample_agent_outcome": count_by(rows, ("sample_id", "agent", "review_outcome")),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, data in summary.items():
        if isinstance(data, list) and (not data or isinstance(data[0], dict)):
            fields = list(data[0].keys()) if data else ["count"]
            write_csv(output_dir / f"{name}.csv", data, fields)
    print(json.dumps({"output_dir": str(output_dir), "total_rows": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
