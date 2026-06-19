#!/usr/bin/env python3
"""Run injection review matrices for SWE-bench code agents.

This runner plans one context per (agent, task, dataset sample, injection step),
starts the shared router, runs Docker containers sequentially, and then calls
manager.analysis.analyze_run(). It supports two important experiment shapes:

- step 0 + replace: 覆盖第一条真正携带用户任务 prompt 的主响应；Claude 的 title/bootstrap 请求不计入 step。
- step N + append/replace: 先从 clean replay trace 复现前 N 个主请求，再在第 N
  个响应注入；这种模式必须传 --trace-source-root 或单任务 --trace-source-dir。

Data layout under <artifacts-root>/<run-id>:
- router/effective_config.json: context registry and API keys for the router
- router/uvicorn.log: router process log
- experiments/<context-id>/manifest.json: public context manifest
- experiments/<context-id>/container/: stdout/stderr/final.patch/docker inspect
- experiments/<context-id>/router/traces/: request, response, injection, reviewer traces
- analysis/summary.json, details.csv, details.jsonl: analyzer outputs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from manager.analysis import analyze_run
from manager.executor import ExperimentExecutor, context_result_from_container, write_context_result
from manager.manifest import write_manifests
from manager.orchestrator import ExperimentOrchestrator
from manager.planner import MatrixPlanOptions, build_matrix_plan, load_dataset_sample_ids, load_task_instance_ids, load_jsonl, parse_agents


def main(default_agents: str = "claude,codex", expose_agents: bool = True) -> int:
    args = parse_args(default_agents=default_agents, expose_agents=expose_agents)
    agents = parse_agents(args.agents)
    run_id = args.run_id or default_run_id(agents, args.injection_step, args.injection_mode)
    artifacts_root = args.artifacts_root.resolve()
    run_root = artifacts_root / run_id
    samples = resolve_samples(args.dataset, args.samples)
    instance_ids = load_task_instance_ids(args.tasks, limit=args.task_limit, selected=args.instance_id)
    validate_trace_source(args, agents, instance_ids)

    options = MatrixPlanOptions(
        run_id=run_id,
        router_mode="replay-inject",
        agents=agents,
        agent_mode="auto_review",
        instance_ids=instance_ids,
        dataset_sample_ids=load_dataset_sample_ids(args.dataset, samples),
        injection_turns=(args.injection_step,),
        injection_mode=args.injection_mode,
        payload_execution_allowed=True,
        timeout_seconds=args.timeout_seconds,
        trace_source_root=args.trace_source_root.resolve() if args.trace_source_root else None,
    )
    plan = build_matrix_plan(options)
    if args.trace_source_dir:
        plan = with_direct_trace_source(plan, args.trace_source_dir.resolve())
    registry_path = write_manifests(artifacts_root, plan, "replay-inject")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "registry_path": str(registry_path),
                "contexts": len(agents) * len(instance_ids) * len(options.dataset_sample_ids),
                "injection_step": args.injection_step,
                "injection_mode": args.injection_mode,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    if args.dry_run:
        summary = analyze_run(run_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0

    router = start_router(args, run_root, registry_path)
    try:
        run_containers(args, run_root)
    finally:
        stop_router(router)

    summary = analyze_run(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


def parse_args(*, default_agents: str, expose_agents: bool) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run replay-inject review matrix for attack samples.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    parser.add_argument("--task-limit", type=int, default=1, help="Default is 1 task; set 0 with --instance-id or for all tasks.")
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--samples", default="all", help="Dataset selector, e.g. all, 1:10, malexec-001")
    parser.add_argument("--injection-step", type=int, default=0, help="Task turn index where injection is applied; step 0 is the first main request carrying the user task prompt.")
    parser.add_argument("--injection-mode", choices=["append", "replace"], default="replace")
    parser.add_argument("--trace-source-root", type=Path, default=None, help="Root laid out as <root>/<agent>/<safe-instance>/router/traces for replay modes.")
    parser.add_argument("--trace-source-dir", type=Path, default=None, help="Direct trace dir for a single agent and a single task, e.g. an existing record run's router/traces.")
    if expose_agents:
        parser.add_argument("--agents", default=default_agents)
    parser.add_argument("--router-host", default="127.0.0.1")
    parser.add_argument("--router-port", type=int, default=18080)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument("--router-start-timeout", type=float, default=20.0)
    parser.add_argument("--agent-tool-image", default="code-agent-router:local")
    parser.add_argument("--network", default="host")
    parser.add_argument("--skip-image-build", action="store_true")
    parser.add_argument("--container-start-delay-seconds", type=float, default=1.0, help="Sleep after each container to reduce load on weak CPUs.")
    parser.add_argument("--force-image-rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not expose_agents:
        args.agents = default_agents
    return args


def default_run_id(agents: tuple[str, ...], injection_step: int, injection_mode: str) -> str:
    agent_part = "-".join(agents)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{agent_part}-auto-review-step{injection_step}-{injection_mode}-{timestamp}"


def validate_trace_source(args: argparse.Namespace, agents: tuple[str, ...], instance_ids: tuple[str, ...]) -> None:
    direct_step0_replace = args.injection_step == 0 and args.injection_mode == "replace"
    has_trace_source = bool(args.trace_source_root or args.trace_source_dir)
    if not direct_step0_replace and not has_trace_source:
        raise SystemExit("--trace-source-root or --trace-source-dir is required unless using --injection-step 0 --injection-mode replace")
    if args.trace_source_root and args.trace_source_dir:
        raise SystemExit("use only one of --trace-source-root or --trace-source-dir")
    if args.trace_source_dir and (len(agents) != 1 or len(instance_ids) != 1):
        raise SystemExit("--trace-source-dir is only valid for one agent and one SWE-bench task")
    for trace_path in (args.trace_source_root, args.trace_source_dir):
        if trace_path and not trace_path.exists():
            raise SystemExit(f"trace source does not exist: {trace_path}")


def with_direct_trace_source(plan, trace_source_dir: Path):
    # 中文注释：单任务 step3 append 通常复用一个已有 record trace；这里把它显式写入每个样本的 manifest。
    return {api_key: dataclass_replace(manifest, trace_source_dir=str(trace_source_dir)) for api_key, manifest in plan.items()}


def resolve_samples(dataset: Path, selector: str) -> str:
    if selector != "all":
        return selector
    rows = load_jsonl(dataset)
    return f"1:{len(rows)}"


def start_router(args: argparse.Namespace, run_root: Path, registry_path: Path) -> subprocess.Popen[bytes]:
    router_dir = run_root / "router"
    router_dir.mkdir(parents=True, exist_ok=True)
    log_path = router_dir / "uvicorn.log"
    env = os.environ.copy()
    env.update(
        {
            "ROUTER_CONTEXT_REGISTRY": str(registry_path),
            "ROUTER_ARTIFACT_ROOT": str(args.artifacts_root.resolve()),
            "ATTACK_DATASET_PATH": str(args.dataset.resolve()),
        }
    )
    log = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "router.app:app", "--host", args.router_host, "--port", str(args.router_port)],
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
            request = Request(url, method="GET")
            with urlopen(request, timeout=1.0) as response:
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
        print(json.dumps({"context_id": run_spec.context_id, "agent": run_spec.agent, "sample": sample_from_context_id(run_spec.context_id), "stage": "start"}, ensure_ascii=False), flush=True)
        if not args.skip_image_build:
            image_result = executor.build_wrapper_image(run_spec)
            if image_result.exit_code != 0:
                result = context_result_from_container(image_result)
                write_context_result(run_spec, result)
                print(json.dumps({"context_id": run_spec.context_id, "stage": "image_build_failed", "exit_code": image_result.exit_code}, ensure_ascii=False), flush=True)
                continue
        run_result = executor.runner.run(run_spec)
        result = context_result_from_container(run_result)
        write_context_result(run_spec, result)
        print(json.dumps({"context_id": run_spec.context_id, "stage": "done", "status": result.status, "exit_code": run_result.exit_code}, ensure_ascii=False), flush=True)
        if args.container_start_delay_seconds > 0:
            time.sleep(args.container_start_delay_seconds)


def sample_from_context_id(context_id: str) -> str:
    marker = "auto_review-"
    if marker not in context_id:
        return ""
    rest = context_id.split(marker, 1)[1]
    return rest.rsplit("-turn-", 1)[0]


if __name__ == "__main__":
    raise SystemExit(main())
