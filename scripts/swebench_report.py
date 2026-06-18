#!/usr/bin/env python3
"""Print concise SWE-bench run reports from summary.json files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def row_for(run_dir: Path) -> dict[str, Any]:
    summary = read_json(run_dir / "summary.json")
    run = summary.get("run") or read_json(run_dir / "run.json")
    agents = summary.get("agents") or {}
    evaluation = summary.get("evaluation")
    if not agents:
        return {
            "run_id": run_dir.name,
            "agent": None,
            "generation": {},
            "patches": {},
            "swebench": {},
            "evaluation": evaluation,
            "predictions": summary.get("prediction_count"),
            "router_traces": summary.get("router_trace_count"),
        }
    # Most baseline runs are one-agent runs. Emit one row per agent if needed.
    rows = []
    for agent, agent_summary in sorted(agents.items()):
        rows.append(
            {
                "run_id": run.get("run_id") or run_dir.name,
                "agent": agent,
                "generation": agent_summary.get("status") or {},
                "patches": agent_summary.get("patches") or {},
                "swebench": agent_summary.get("swebench") or {},
                "evaluation": evaluation if not evaluation or evaluation.get("agent") == agent else None,
                "predictions": summary.get("prediction_count"),
                "router_traces": summary.get("router_trace_count"),
            }
        )
    return {"rows": rows}


def flatten(run_dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        item = row_for(run_dir)
        if "rows" in item:
            rows.extend(item["rows"])
        else:
            rows.append(item)
    return rows


def fmt_counter(value: dict[str, Any]) -> str:
    if not value:
        return "-"
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def print_markdown(rows: list[dict[str, Any]]) -> None:
    print("| Run | Agent | Generation | Patches | SWE-bench | Predictions | Router traces |")
    print("| --- | --- | --- | --- | --- | ---: | ---: |")
    for row in rows:
        evaluation = row.get("evaluation") or {}
        swebench = row.get("swebench") or {}
        if evaluation:
            swebench = {
                "resolved": evaluation.get("resolved"),
                "unresolved": evaluation.get("unresolved"),
                "empty_patch": evaluation.get("empty_patch"),
                "errors": evaluation.get("errors"),
            }
        print(
            "| {run} | {agent} | {generation} | {patches} | {swebench} | {predictions} | {router_traces} |".format(
                run=row.get("run_id") or "-",
                agent=row.get("agent") or "-",
                generation=fmt_counter(row.get("generation") or {}),
                patches=fmt_counter(row.get("patches") or {}),
                swebench=fmt_counter({k: v for k, v in swebench.items() if v is not None}),
                predictions=row.get("predictions") if row.get("predictions") is not None else "-",
                router_traces=row.get("router_traces") if row.get("router_traces") is not None else "-",
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", nargs="+", type=Path)
    parser.add_argument("--format", choices=["md", "json"], default="md")
    args = parser.parse_args()
    rows = flatten(args.run_dir)
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_markdown(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
