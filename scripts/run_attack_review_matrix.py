#!/usr/bin/env python3
"""Run direct and SWE-bench attack review matrices with resumable artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_PATH = Path("data/code_agent_attack_dataset.jsonl")
DIRECT_ROOT = Path("artifacts/agent-review-gate")
SWEBENCH_MATRIX = Path("scripts/swebench_attack_matrix.py")
SUMMARY_SCRIPT = Path("scripts/summarize_attack_review_outcomes.py")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def safe_token(value: str) -> str:
    chars = [ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value]
    return "".join(chars).strip("-._") or "unknown"


def parse_samples(spec: str, dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(row.get("id")): row for row in dataset}
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in (item.strip() for item in spec.split(",") if item.strip()):
        selectors: list[str]
        if ":" in part:
            start_s, end_s = part.split(":", 1)
            start = int(start_s)
            end = int(end_s)
            if start < 1 or end < start:
                raise SystemExit(f"invalid sample range: {part}")
            selectors = [str(index) for index in range(start, end + 1)]
        else:
            selectors = [part]
        for selector in selectors:
            if selector.isdigit():
                index = int(selector) - 1
                if index < 0 or index >= len(dataset):
                    raise SystemExit(f"sample index out of range: {selector}")
                row = dataset[index]
                env_value = selector
            else:
                row = by_id.get(selector)
                if row is None:
                    raise SystemExit(f"unknown sample id: {selector}")
                env_value = selector
            sample_id = str(row.get("id"))
            key = f"{env_value}:{sample_id}"
            if key in seen:
                continue
            seen.add(key)
            samples.append(
                {
                    "selector": env_value,
                    "id": sample_id,
                    "category": row.get("category"),
                    "subcategory": row.get("subcategory"),
                    "tool_name": row.get("tool_name"),
                    "severity": row.get("severity"),
                }
            )
    return samples


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def direct_run_id(prefix: str, sample: dict[str, Any]) -> str:
    return f"{prefix}-direct-{safe_token(str(sample['id']))}"


def direct_done(run_id: str) -> bool:
    return (DIRECT_ROOT / run_id / "analysis" / "agent-attack-analysis.json").exists()


def run_direct_sample(args: argparse.Namespace, sample: dict[str, Any], manifest_dir: Path) -> int:
    run_id = direct_run_id(args.run_id_prefix, sample)
    run_dir = DIRECT_ROOT / run_id
    record = {
        "kind": "direct",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "sample": sample,
        "started_at": utc_now(),
    }
    if args.skip_existing and direct_done(run_id):
        record.update({"status": "skipped_existing", "ended_at": utc_now(), "exit_code": 0})
        append_jsonl(manifest_dir / "results.jsonl", record)
        return 0

    env = os.environ.copy()
    env.update(
        {
            "RUN_ID": run_id,
            "ATTACK_SAMPLE": str(sample["selector"]),
            "CODEX_MODES": "auto-review" if "codex" in args.agents else "",
            "CLAUDE_MODES": "auto" if "claude" in args.agents else "",
            "CODEX_TIMEOUT_SECONDS": str(args.direct_timeout),
            "CLAUDE_TIMEOUT_SECONDS": str(args.direct_timeout),
            "BUILD_ROUTER": args.build_router,
            "ATTACK_CODEX_SANDBOX_PERMISSIONS": "require_escalated",
            "ATTACK_CODEX_APPROVAL_JUSTIFICATION": "Approve controlled direct Codex auto-review experiment payload inside the isolated Docker lab.",
        }
    )
    cmd = ["scripts/agent-review-gate-lab.sh"]
    record["cmd"] = cmd
    append_jsonl(manifest_dir / "commands.jsonl", {**record, "env_subset": {k: env[k] for k in env if k.startswith(("RUN_ID", "ATTACK_", "CODEX_", "CLAUDE_", "BUILD_"))}})
    print(json.dumps({"event": "start", "kind": "direct", "run_id": run_id, "sample": sample["id"]}, ensure_ascii=False), flush=True)
    code = subprocess.call(cmd, env=env)
    record.update({"exit_code": code, "ended_at": utc_now()})
    if code == 0 and run_dir.exists():
        analysis_code = subprocess.call([sys.executable, "scripts/analyze_agent_attack_results.py", str(run_dir)])
        record["analysis_exit_code"] = analysis_code
    append_jsonl(manifest_dir / "results.jsonl", record)
    return code


def run_swebench_turn(args: argparse.Namespace, turn: str, manifest_dir: Path) -> int:
    prefix = f"{args.run_id_prefix}-swebench-turn{safe_token(turn)}"
    cmd = [
        sys.executable,
        str(SWEBENCH_MATRIX),
        "--run-id-prefix",
        prefix,
        "--agents",
        ",".join(args.agents),
        "--mode",
        "auto-review",
        "--task-limit",
        str(args.task_limit),
        "--samples",
        args.samples,
        "--timeout",
        str(args.swebench_timeout),
        "--no-baseline",
        "--append",
        "--attack-injection-turn",
        str(turn),
        "--stop-after-attack-observed",
        "--execute",
    ]
    if args.env_file:
        cmd.extend(["--env-file", str(args.env_file)])
    if args.skip_existing:
        cmd.append("--skip-existing")
    else:
        cmd.append("--no-skip-existing")
    record = {"kind": "swebench", "turn": turn, "prefix": prefix, "cmd": cmd, "started_at": utc_now()}
    append_jsonl(manifest_dir / "commands.jsonl", record)
    print(json.dumps({"event": "start", "kind": "swebench", "turn": turn, "prefix": prefix}, ensure_ascii=False), flush=True)
    env = os.environ.copy()
    env["CODEX_DOCKER_PRIVILEGED"] = os.environ.get("CODEX_DOCKER_PRIVILEGED", "true")
    code = subprocess.call(cmd, env=env)
    append_jsonl(manifest_dir / "results.jsonl", {**record, "exit_code": code, "ended_at": utc_now()})
    return code


def run_swebench_baseline(args: argparse.Namespace, manifest_dir: Path) -> int:
    prefix = f"{args.run_id_prefix}-swebench-baseline"
    cmd = [
        sys.executable,
        str(SWEBENCH_MATRIX),
        "--run-id-prefix",
        prefix,
        "--agents",
        ",".join(args.agents),
        "--mode",
        "auto-review",
        "--task-limit",
        str(args.task_limit),
        "--timeout",
        str(args.baseline_timeout),
        "--no-append",
        "--baseline",
        "--execute",
    ]
    if args.env_file:
        cmd.extend(["--env-file", str(args.env_file)])
    if args.skip_existing:
        cmd.append("--skip-existing")
    else:
        cmd.append("--no-skip-existing")
    record = {"kind": "swebench_baseline", "prefix": prefix, "cmd": cmd, "started_at": utc_now()}
    append_jsonl(manifest_dir / "commands.jsonl", record)
    print(json.dumps({"event": "start", "kind": "swebench_baseline", "prefix": prefix}, ensure_ascii=False), flush=True)
    env = os.environ.copy()
    env["CODEX_DOCKER_PRIVILEGED"] = os.environ.get("CODEX_DOCKER_PRIVILEGED", "true")
    code = subprocess.call(cmd, env=env)
    append_jsonl(manifest_dir / "results.jsonl", {**record, "exit_code": code, "ended_at": utc_now()})
    return code


def run_summary(args: argparse.Namespace, manifest_dir: Path) -> int:
    cmd = [sys.executable, str(SUMMARY_SCRIPT), "--run-id-prefix", args.run_id_prefix]
    print(json.dumps({"event": "start", "kind": "summary", "cmd": cmd}, ensure_ascii=False), flush=True)
    code = subprocess.call(cmd)
    append_jsonl(manifest_dir / "results.jsonl", {"kind": "summary", "cmd": cmd, "exit_code": code, "ended_at": utc_now()})
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id-prefix", default=datetime.now().strftime("%Y%m%d-%H%M%S-full-review-matrix"))
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--samples", default="1:400")
    parser.add_argument("--agents", default="codex,claude", help="Comma-separated subset: codex,claude")
    parser.add_argument("--positions", default="1,3,5", help="Comma-separated ATTACK_INJECTION_TURN values for SWE-bench append")
    parser.add_argument("--task-limit", type=int, default=3)
    parser.add_argument("--direct-timeout", type=int, default=90)
    parser.add_argument("--swebench-timeout", type=int, default=240)
    parser.add_argument("--baseline-timeout", type=int, default=1800)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--build-router", default="auto")
    parser.add_argument("--direct", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--swebench", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--swebench-baseline", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--continue-on-error", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--summarize", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    agents = [item.strip() for item in args.agents.split(",") if item.strip()]
    if any(agent not in {"codex", "claude"} for agent in agents):
        raise SystemExit(f"unsupported agents: {args.agents}")
    args.agents = agents

    samples = parse_samples(args.samples, read_jsonl(args.dataset))
    positions = [item.strip() for item in args.positions.split(",") if item.strip()]
    manifest_dir = Path("artifacts/attack-review-matrices") / args.run_id_prefix
    manifest = {
        "run_id_prefix": args.run_id_prefix,
        "created_at": utc_now(),
        "execute": args.execute,
        "direct": args.direct,
        "swebench": args.swebench,
        "agents": agents,
        "samples": samples,
        "sample_count": len(samples),
        "positions": positions,
        "task_limit": args.task_limit,
        "direct_timeout": args.direct_timeout,
        "swebench_timeout": args.swebench_timeout,
        "swebench_baseline": args.swebench_baseline,
        "baseline_timeout": args.baseline_timeout,
        "notes": [
            "direct uses router replace/tool_call via scripts/agent-review-gate-lab.sh",
            "direct Codex replace/tool_call may execute a returned command item without consulting the auto-reviewer; classify by observed transcript, not by intended sandbox_permissions metadata",
            "optional swebench baseline uses ATTACK_INJECTION_MODE=off as a no-injection control group",
            "swebench append runs stop each task after the injected tool call is observed to avoid measuring full solve time",
            "swebench uses append injection into official task images via scripts/swebench_attack_matrix.py",
            "Codex auto-review cases set sandbox_permissions=require_escalated to exercise Approve for me",
        ],
    }
    write_json(manifest_dir / "matrix.json", manifest)
    if not args.execute:
        print(json.dumps({"matrix_dir": str(manifest_dir), "direct_cases": len(samples) if args.direct else 0, "swebench_baseline": bool(args.swebench_baseline), "swebench_matrices": len(positions) if args.swebench else 0, "execute": False}, ensure_ascii=False))
        return 0

    if args.direct:
        for sample in samples:
            code = run_direct_sample(args, sample, manifest_dir)
            if code != 0 and not args.continue_on_error:
                return code
    if args.swebench_baseline:
        code = run_swebench_baseline(args, manifest_dir)
        if code != 0 and not args.continue_on_error:
            return code
    if args.swebench:
        for turn in positions:
            code = run_swebench_turn(args, turn, manifest_dir)
            if code != 0 and not args.continue_on_error:
                return code
    if args.summarize:
        return run_summary(args, manifest_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
