#!/usr/bin/env python3
"""Run Claude Code in proxy-record mode with allow_all permissions.

Pipeline used by this script (mirrors run_claude_auto_review_matrix.py but
without injection):
1. Plan a single record context with manager.planner:
   - agent = claude only
   - agent_mode = allow_all, mapped by ClaudeAdapter to
     `claude --permission-mode acceptEdits --print ...`
   - router_mode = proxy-record (passes every request straight through to the
     upstream provider and writes the canonical request/response to disk)
   - injection_turn = None (record mode never injects)
   - payload_execution_allowed = false (no attack payload is rendered)
2. Start the shared router locally with:
   - ROUTER_CONTEXT_REGISTRY=<artifacts-root>/<run-id>/router/effective_config.json
   - ROUTER_ARTIFACT_ROOT=<artifacts-root>
   - ROUTER_MODE=proxy-record, DEFAULT_PROVIDER=deepseek, DEEPSEEK_MODEL=...
   - ATTACK_DATASET_PATH=<dataset> (only used by replay-inject stages)
3. Run the planned SWE-bench container sequentially through Docker.
4. Print router log path so the orchestrator can pick up the produced trace.

The output trace lives at:
  artifacts/runs/<run-id>/experiments/<run-id>-<instance>-claude-allow_all-record-record/router/traces

Use this path as --trace-source-dir for subsequent replay-inject runs.

Important parameters:
- --run-id: defaults to claude-record-allow-all-<timestamp>
- --instance-id: defaults to sqlfluff__sqlfluff-2419
- --timeout-seconds: defaults to 360
- --container-start-delay-seconds: keep >0 on weak CPU machines
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from manager.executor import ExperimentExecutor, context_result_from_container, write_context_result
from manager.orchestrator import ExperimentOrchestrator
from manager.planner import MatrixPlanOptions, build_matrix_plan, load_task_instance_ids


def main() -> int:
    args = parse_args()
    run_id = args.run_id
    artifacts_root = args.artifacts_root.resolve()
    run_root = artifacts_root / run_id

    instance_ids = load_task_instance_ids(args.tasks, limit=1, selected=[args.instance_id])
    if len(instance_ids) != 1:
        raise SystemExit(f"record mode supports exactly one task, got {instance_ids}")

    options = MatrixPlanOptions(
        run_id=run_id,
        router_mode="proxy-record",
        agents=("claude",),
        agent_mode="allow_all",
        instance_ids=instance_ids,
        dataset_sample_ids=("record",),
        injection_turns=(None,),
        injection_mode="append",
        payload_execution_allowed=False,
        timeout_seconds=args.timeout_seconds,
    )
    plan = build_matrix_plan(options)
    if len(plan) != 1:
        raise SystemExit(f"record plan must have exactly one context, got {len(plan)}")
    context_id = next(iter(plan.values())).context_id

    from manager.manifest import write_manifests

    registry_path = write_manifests(artifacts_root, plan, "proxy-record")
    print(
        f"[record] run_id={run_id} context_id={context_id} registry={registry_path}",
        flush=True,
    )

    if args.dry_run:
        print("[record] dry-run: planned manifests without starting router or containers")
        return 0

    router = start_router(args, run_root, registry_path)
    try:
        run_containers(args, run_root)
    finally:
        stop_router(router)

    trace_dir = run_root / "experiments" / context_id / "router" / "traces"
    response_count = sum(1 for _ in trace_dir.glob("*/response.upstream.json")) if trace_dir.exists() else 0
    print(
        f"[record] done: trace_dir={trace_dir} response_artifacts={response_count}",
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Claude proxy-record trace with allow_all mode.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    parser.add_argument("--instance-id", default="sqlfluff__sqlfluff-2419")
    parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    parser.add_argument("--router-host", default="127.0.0.1")
    parser.add_argument("--router-port", type=int, default=18080)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument("--router-start-timeout", type=float, default=30.0)
    parser.add_argument("--agent-tool-image", default="code-agent-router:local")
    parser.add_argument("--network", default="host")
    parser.add_argument("--skip-image-build", action="store_true")
    parser.add_argument("--container-start-delay-seconds", type=float, default=1.0)
    parser.add_argument("--force-image-rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def default_run_id(stamp: str) -> str:
    return f"claude-record-allow-all-{stamp}"


def start_router(args: argparse.Namespace, run_root: Path, registry_path: Path) -> subprocess.Popen[bytes]:
    router_dir = run_root / "router"
    router_dir.mkdir(parents=True, exist_ok=True)
    log_path = router_dir / "uvicorn.log"
    env = os.environ.copy()
    env.update(
        {
            "ROUTER_MODE": "proxy-record",
            "DEFAULT_PROVIDER": "deepseek",
            "ROUTER_CONTEXT_REGISTRY": str(registry_path),
            "ROUTER_ARTIFACT_ROOT": str(args.artifacts_root.resolve()),
            "ROUTER_AGENT_MODE": "allow_all",
            "ATTACK_DATASET_PATH": str(args.dataset.resolve()),
        }
    )
    log = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "router.app:app",
            "--host",
            args.router_host,
            "--port",
            str(args.router_port),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    wait_for_router(args, process, log_path)
    return process


def wait_for_router(args: argparse.Namespace, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + args.router_start_timeout
    url = f"http://{args.router_host}:{args.router_port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"router exited early with code {process.returncode}; see {log_path}")
        try:
            with urlopen(Request(url, method="GET"), timeout=1.0) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.2)
    raise TimeoutError(f"router did not become healthy within {args.router_start_timeout}s; see {log_path}")


def stop_router(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_containers(args: argparse.Namespace, run_root: Path) -> None:
    orchestrator = ExperimentOrchestrator(
        run_root=run_root,
        router_url=f"http://{args.router_host}:{args.router_port}",
        tasks_path=args.tasks,
        prompt_template_path=Path("bench/prompts/solve_issue.md"),
    )
    executor = ExperimentExecutor(
        orchestrator=orchestrator,
        agent_tool_image=args.agent_tool_image,
        force_image_rebuild=bool(args.force_image_rebuild),
    )
    for spec in orchestrator.build_container_specs():
        run_spec = dataclass_replace(spec, network=args.network)
        print(f"[record] container start: {run_spec.context_id}", flush=True)
        if not args.skip_image_build:
            image_result = executor.build_wrapper_image(run_spec)
            if image_result.exit_code != 0:
                result = context_result_from_container(image_result)
                write_context_result(run_spec, result)
                print(
                    f"[record] image_build_failed: {run_spec.context_id} exit={image_result.exit_code}",
                    flush=True,
                )
                continue
        run_result = executor.runner.run(run_spec)
        result = context_result_from_container(run_result)
        write_context_result(run_spec, result)
        print(
            f"[record] container done: {run_spec.context_id} exit={run_result.exit_code}",
            flush=True,
        )
        if args.container_start_delay_seconds > 0:
            time.sleep(args.container_start_delay_seconds)


if __name__ == "__main__":
    if "--run-id" not in sys.argv:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sys.argv.extend(["--run-id", default_run_id(stamp)])
    raise SystemExit(main())
