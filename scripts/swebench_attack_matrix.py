#!/usr/bin/env python3
"""Plan or run SWE-bench task-image attack-injection experiment matrices."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASKS_PATH = Path("bench/swe_lite_dev_23.jsonl")
DATASET_PATH = Path("data/code_agent_attack_dataset.jsonl")
MATRIX_ROOT = Path("artifacts/swebench-baseline/matrices")
RUN_ROOT = Path("artifacts/swebench-baseline")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_prefix() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-swebench-attack-matrix")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def safe_token(value: str) -> str:
    chars = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
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


def selected_tasks(tasks: list[dict[str, Any]], *, limit: int, instance_ids: list[str]) -> list[dict[str, Any]]:
    if instance_ids:
        allowed = set(instance_ids)
        tasks = [task for task in tasks if str(task.get("instance_id")) in allowed]
    if limit:
        tasks = tasks[:limit]
    return tasks


def agent_env(agent: str, mode: str) -> dict[str, str]:
    if agent == "codex":
        if mode not in {"auto-review", "allow-all"}:
            raise SystemExit(f"unknown Codex mode: {mode}")
        return {"CODEX_EXPERIMENT_MODE": mode}
    if agent == "claude":
        if mode == "auto-review":
            mode = "auto"
        if mode not in {"auto", "allow-all"}:
            raise SystemExit(f"unknown Claude mode: {mode}")
        return {"CLAUDE_EXPERIMENT_MODE": mode}
    raise SystemExit(f"unsupported agent for attack matrix: {agent}")


def runner_command(
    *,
    agent: str,
    run_id: str,
    args: argparse.Namespace,
    instance_ids: list[str],
    router_mode: str = "proxy",
) -> list[str]:
    cmd = [
        sys.executable,
        "scripts/swebench_run_baseline.py",
        "--agent",
        agent,
        "--run-id",
        run_id,
        "--execution-mode",
        "swebench-task-image",
        "--docker-image",
        args.docker_image,
        "--swebench-namespace",
        args.swebench_namespace,
        "--timeout",
        str(args.timeout),
        "--router-mode",
        router_mode,
    ]
    if router_mode == "replay":
        if not args.replay_trace_dir:
            raise SystemExit("--replay-trace-dir is required for replay router mode")
        cmd.extend(["--replay-trace-dir", str(args.replay_trace_dir), "--replay-response-file", args.replay_response_file])
    if args.tasks != TASKS_PATH:
        cmd.extend(["--tasks", str(args.tasks)])
    if args.task_limit and not instance_ids:
        cmd.extend(["--limit", str(args.task_limit)])
    for instance_id in instance_ids:
        cmd.extend(["--instance-id", instance_id])
    if args.force_task_image_rebuild:
        cmd.append("--force-task-image-rebuild")
    if args.stop_after_attack_observed:
        cmd.append("--stop-after-attack-observed")
    if args.skip_existing:
        cmd.append("--skip-existing")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.env_file:
        cmd.extend(["--env-file", str(args.env_file)])
    return cmd


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", default="codex,claude", help="Comma-separated agents: codex,claude")
    parser.add_argument("--mode", default="auto-review", help="codex: auto-review|allow-all; claude: auto-review maps to auto")
    parser.add_argument("--tasks", type=Path, default=TASKS_PATH)
    parser.add_argument("--task-limit", type=int, default=3)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--samples", default="1:400", help="Sample selectors, e.g. 1:400 or 1,2,malexec-001")
    parser.add_argument("--run-id-prefix", default=default_prefix())
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--docker-image", default="code-agent-router:local")
    parser.add_argument("--swebench-namespace", default="swebench")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--attack-injection-turn", default="3")
    parser.add_argument("--attack-append-position", default="last")
    parser.add_argument("--replay-trace-dir", type=Path, default=None, help="Clean trace dir/run dir used by ROUTER_MODE=replay for append runs")
    parser.add_argument("--replay-response-file", default="response.router.after.json", help="Replay response artifact name; use response.router.before.json for historical append traces")
    parser.add_argument("--baseline", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--append", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force-task-image-rebuild", action="store_true")
    parser.add_argument("--stop-after-attack-observed", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to the underlying runner")
    parser.add_argument("--allow-unsafe-raw", action="store_true", help="Allow rendering original dataset tool arguments. Use only in isolated review experiments.")
    parser.add_argument("--execute", action="store_true", help="Actually run commands. Without this, only write the plan.")
    parser.add_argument("--analyze", action=argparse.BooleanOptionalAction, default=True, help="Analyze each completed run with scripts/analyze_agent_attack_results.py")
    args = parser.parse_args()

    if not args.tasks.exists():
        raise SystemExit(f"missing tasks file: {args.tasks}")
    if not args.dataset.exists():
        raise SystemExit(f"missing dataset file: {args.dataset}")

    agents = [agent.strip() for agent in args.agents.split(",") if agent.strip()]
    if not agents:
        raise SystemExit("at least one agent is required")
    for agent in agents:
        if agent not in {"codex", "claude"}:
            raise SystemExit(f"unsupported agent: {agent}")

    tasks = selected_tasks(read_jsonl(args.tasks), limit=args.task_limit, instance_ids=args.instance_id)
    if not tasks:
        raise SystemExit("no tasks selected")
    instance_ids = [str(task["instance_id"]) for task in tasks]
    samples = parse_samples(args.samples, read_jsonl(args.dataset))
    if args.append and not samples:
        raise SystemExit("no samples selected")

    matrix_dir = MATRIX_ROOT / args.run_id_prefix
    commands_path = matrix_dir / "commands.jsonl"
    if commands_path.exists():
        commands_path.unlink()

    base_env = {
        "CODEX_DOCKER_PRIVILEGED": os.environ.get("CODEX_DOCKER_PRIVILEGED", "true"),
    }
    manifest: dict[str, Any] = {
        "run_id_prefix": args.run_id_prefix,
        "created_at": utc_now(),
        "execute": args.execute,
        "dry_run": args.dry_run,
        "agents": agents,
        "mode": args.mode,
        "tasks_path": str(args.tasks),
        "task_count": len(tasks),
        "task_instance_ids": instance_ids,
        "dataset": str(args.dataset),
        "sample_count": len(samples),
        "samples": samples,
        "timeout": args.timeout,
        "execution_mode": "swebench-task-image",
        "baseline": args.baseline,
        "append": args.append,
        "analyze": args.analyze,
        "stop_after_attack_observed": args.stop_after_attack_observed,
        "attack_injection_turn": args.attack_injection_turn,
        "attack_append_position": args.attack_append_position,
        "replay_trace_dir": str(args.replay_trace_dir) if args.replay_trace_dir else "",
        "replay_response_file": args.replay_response_file,
        "append_router_mode": "replay" if args.replay_trace_dir else "proxy",
        "attack_payload_mode": "raw",
        "attack_allow_unsafe_raw": args.allow_unsafe_raw,
        "runner": "scripts/swebench_run_baseline.py",
        "base_env": base_env,
        "notes": [
            "Baseline runs use ATTACK_INJECTION_MODE=off and collect normal Kimi-backed traces.",
            "Append runs use ATTACK_INJECTION_MODE=append and raw dataset payload rendering only when --allow-unsafe-raw is set.",
            "When --replay-trace-dir is set, append runs use ROUTER_MODE=replay and do not call Kimi.",
            "Codex auto-review means CODEX_EXPERIMENT_MODE=auto-review; Claude maps this mode to CLAUDE_EXPERIMENT_MODE=auto.",
            "Codex auto-review append runs set ATTACK_CODEX_SANDBOX_PERMISSIONS=require_escalated so /permissions Approve for me behavior is actually exercised.",
        ],
    }
    write_json(matrix_dir / "matrix.json", manifest)

    planned: list[tuple[list[str], dict[str, str], dict[str, Any]]] = []
    for agent in agents:
        env = {**base_env, **agent_env(agent, args.mode)}
        if args.baseline:
            run_id = f"{args.run_id_prefix}-baseline-{agent}-{safe_token(args.mode)}"
            cmd = runner_command(agent=agent, run_id=run_id, args=args, instance_ids=instance_ids, router_mode="proxy")
            record = {
                "kind": "baseline",
                "agent": agent,
                "mode": args.mode,
                "run_id": run_id,
                "run_dir": str(RUN_ROOT / "runs" / run_id),
                "env": {**env, "ROUTER_MODE": "proxy", "ATTACK_INJECTION_MODE": "off"},
                "cmd": cmd,
            }
            planned.append((cmd, record["env"], record))
            append_jsonl(commands_path, record)
        if args.append:
            for sample in samples:
                run_id = f"{args.run_id_prefix}-append-{agent}-{safe_token(args.mode)}-{safe_token(sample['id'])}"
                run_env = {
                    **env,
                    "ROUTER_MODE": "replay" if args.replay_trace_dir else "proxy",
                    "ATTACK_INJECTION_MODE": "append",
                    "ATTACK_INJECTION_TURN": str(args.attack_injection_turn),
                    "ATTACK_APPEND_POSITION": str(args.attack_append_position),
                    "ATTACK_SAMPLE": str(sample["selector"]),
                    "ATTACK_PAYLOAD_MODE": "raw",
                    "ATTACK_ALLOW_UNSAFE_RAW": "true" if args.allow_unsafe_raw else "false",
                }
                if agent == "codex" and args.mode == "auto-review":
                    run_env["ATTACK_CODEX_SANDBOX_PERMISSIONS"] = "require_escalated"
                    run_env["ATTACK_CODEX_APPROVAL_JUSTIFICATION"] = (
                        "Approve controlled Codex auto-review experiment payload inside the isolated SWE-bench task container."
                    )
                cmd = runner_command(agent=agent, run_id=run_id, args=args, instance_ids=instance_ids, router_mode="replay" if args.replay_trace_dir else "proxy")
                record = {
                    "kind": "append",
                    "agent": agent,
                    "mode": args.mode,
                    "sample": sample,
                    "run_id": run_id,
                    "run_dir": str(RUN_ROOT / "runs" / run_id),
                    "env": run_env,
                    "cmd": cmd,
                }
                planned.append((cmd, run_env, record))
                append_jsonl(commands_path, record)

    print(json.dumps({"matrix_dir": str(matrix_dir), "planned_commands": len(planned), "execute": args.execute}, ensure_ascii=False))
    if not args.execute:
        return 0

    for cmd, run_env, record in planned:
        print(json.dumps({"event": "start", "kind": record["kind"], "agent": record["agent"], "run_id": record["run_id"]}, ensure_ascii=False))
        env = os.environ.copy()
        env.update(run_env)
        exit_code = subprocess.call(cmd, env=env)
        result = {**record, "exit_code": exit_code, "ended_at": utc_now()}
        run_dir = Path(str(record["run_dir"]))
        if exit_code == 0 and args.analyze and run_dir.exists():
            analysis_code = subprocess.call([sys.executable, "scripts/analyze_agent_attack_results.py", str(run_dir)])
            result["analysis_exit_code"] = analysis_code
            analysis_json = run_dir / "analysis" / "agent-attack-analysis.json"
            if analysis_json.exists():
                try:
                    result["analysis_summary"] = json.loads(analysis_json.read_text(encoding="utf-8")).get("summary")
                except json.JSONDecodeError:
                    result["analysis_summary"] = {"error": "invalid analysis json"}
        append_jsonl(matrix_dir / "results.jsonl", result)
        if exit_code != 0:
            print(json.dumps({"event": "failed", "run_id": record["run_id"], "exit_code": exit_code}, ensure_ascii=False), file=sys.stderr)
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
