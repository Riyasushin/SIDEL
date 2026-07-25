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
from manager.planner import MatrixPlanOptions, build_matrix_plan, load_dataset_sample_ids, load_task_instance_ids, load_jsonl, parse_agents, parse_turns


def main(default_agents: str = "claude,codex", expose_agents: bool = True) -> int:
    args = parse_args(default_agents=default_agents, expose_agents=expose_agents)
    agents = parse_agents(args.agents)
    injection_turns = resolve_injection_turns(args)
    run_id = args.run_id or default_run_id(agents, injection_turns, args.injection_mode, args.injection_turn_mode)
    artifacts_root = args.artifacts_root.resolve()
    run_root = artifacts_root / run_id
    samples = resolve_samples(args.dataset, args.samples)
    instance_ids = load_task_instance_ids(args.tasks, limit=args.task_limit, selected=args.instance_id)
    validate_trace_source(args, agents, instance_ids, injection_turns)

    options = MatrixPlanOptions(
        run_id=run_id,
        router_mode=args.router_mode,
        agents=agents,
        agent_mode=args.agent_mode,
        instance_ids=instance_ids,
        dataset_sample_ids=load_dataset_sample_ids(args.dataset, samples),
        injection_turns=injection_turns,
        injection_mode=args.injection_mode,
        payload_execution_allowed=True,
        timeout_seconds=args.timeout_seconds,
        trace_source_root=args.trace_source_root.resolve() if args.trace_source_root else None,
        replay_match_mode=args.replay_match_mode,
        enable_tool_defense=bool(args.enable_tool_defense),
        dynamic_l3_enabled=bool(args.dynamic_l3),
        dynamic_l4_enabled=bool(args.dynamic_l4),
        injection_turn_mode=args.injection_turn_mode,
        injection_random_seed=args.injection_random_seed,
    )
    plan = build_matrix_plan(options)
    if args.trace_source_dir:
        plan = with_direct_trace_source(plan, args.trace_source_dir.resolve())
    if args.context_id_file:
        selected_context_ids = load_context_id_file(args.context_id_file)
        plan = {
            api_key: manifest
            for api_key, manifest in plan.items()
            if manifest.context_id in selected_context_ids
        }
        if not plan:
            raise SystemExit("--context-id-file did not match any planned contexts")
    if args.skip_successfully_defended_contexts:
        plan = {
            api_key: manifest
            for api_key, manifest in plan.items()
            if not successfully_defended_context(run_root, manifest.context_id)
        }
        if not plan:
            raise SystemExit("all planned contexts are already successfully injected and defense-blocked")
    if args.resume_statuses:
        requested_statuses = {value.strip() for value in args.resume_statuses.split(",") if value.strip()}
        plan = {
            api_key: manifest
            for api_key, manifest in plan.items()
            if context_matches_resume_status(run_root, manifest.context_id, requested_statuses)
        }
        if not plan:
            raise SystemExit("--resume-statuses did not match any planned contexts")
    registry_path = write_manifests(artifacts_root, plan, args.router_mode)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "registry_path": str(registry_path),
                "contexts": len(plan),
                "injection_turn_mode": args.injection_turn_mode,
                "injection_turns": list(int(turn) for turn in injection_turns if turn is not None),
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
        run_containers(args, run_root, {manifest.context_id for manifest in plan.values()})
    finally:
        stop_router(router)

    summary = analyze_run(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


def parse_args(*, default_agents: str, expose_agents: bool) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run replay-inject review matrix for attack samples.")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--router-mode",
        choices=["inject", "replay-inject"],
        default="replay-inject",
        help="Use inject to proxy every pre-injection task turn; replay-inject requires clean trace sources.",
    )
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    parser.add_argument("--task-limit", type=int, default=1, help="Default is 1 task; set 0 with --instance-id or for all tasks.")
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--samples", default="all", help="Dataset selector, e.g. all, 1:10, malexec-001")
    parser.add_argument(
        "--context-id-file",
        type=Path,
        help="Optional newline-delimited context IDs to run; filters the generated matrix exactly.",
    )
    parser.add_argument(
        "--skip-successfully-defended-contexts",
        action="store_true",
        help="Resume only contexts that were not successfully L4-rewritten and defense-blocked in this run.",
    )
    parser.add_argument(
        "--resume-statuses",
        default="",
        help="Comma-separated resume states: unstarted, timeout, or an analysis status such as agent_crashed.",
    )
    parser.add_argument("--injection-step", type=int, default=0, help="Task turn index where injection is applied; step 0 is the first main request carrying the user task prompt.")
    parser.add_argument("--injection-steps", default="", help="Candidate injection turns, e.g. 0,2:4. Required for --injection-turn-mode=random.")
    parser.add_argument("--injection-turn-mode", choices=["fixed", "random"], default="fixed")
    parser.add_argument("--injection-random-seed", type=int, default=None)
    parser.add_argument("--injection-mode", choices=["append", "replace"], default="replace")
    parser.add_argument("--trace-source-root", type=Path, default=None, help="Root laid out as <root>/<agent>/<safe-instance>/router/traces for replay modes.")
    parser.add_argument("--trace-source-dir", type=Path, default=None, help="Direct trace dir for a single agent and a single task, e.g. an existing record run's router/traces.")
    if expose_agents:
        parser.add_argument("--agents", default=default_agents)
    parser.add_argument(
        "--agent-mode",
        choices=["allow_all", "auto_review"],
        default="auto_review",
        help="Agent permission mode: allow_all (--permission-mode acceptEdits) or auto_review (default).",
    )
    parser.add_argument("--router-host", default="127.0.0.1", help="Bind host for the local router process.")
    parser.add_argument(
        "--router-url-host",
        default="",
        help="Host that task containers should use to reach the router. Defaults to --router-host.",
    )
    parser.add_argument("--router-port", type=int, default=18080)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=120.0,
        help="Per-attempt upstream model request timeout; retryable requests are attempted up to three times.",
    )
    parser.add_argument(
        "--request-max-attempts",
        type=int,
        default=0,
        help="Retryable upstream request attempts; 0 retries until the task container times out.",
    )
    parser.add_argument(
        "--replay-match-mode",
        choices=["exact_request_hash", "task_turn_compatible"],
        default="task_turn_compatible",
        help="Replay matching strategy: exact_request_hash (strict historical replay) or task_turn_compatible (recommended for injection experiments).",
    )
    parser.add_argument("--router-start-timeout", type=float, default=20.0)
    parser.add_argument("--agent-tool-image", default="code-agent-router:local")
    parser.add_argument("--network", default="host")
    parser.add_argument("--skip-image-build", action="store_true")
    parser.add_argument("--container-start-delay-seconds", type=float, default=1.0, help="Sleep after each container to reduce load on weak CPUs.")
    parser.add_argument("--force-image-rebuild", action="store_true")
    parser.add_argument("--enable-tool-defense", action="store_true", help="Enable the response tool-call defense for every planned context.")
    parser.add_argument("--dynamic-l3", action="store_true", help="Rewrite injected assistant text at runtime using current task trajectory.")
    parser.add_argument("--dynamic-l3-backend", choices=["deepseek_chat", "call_versions_official"], default="deepseek_chat")
    parser.add_argument("--dynamic-l3-call-version", type=int, default=1)
    parser.add_argument("--dynamic-l3-alpha", type=float, default=0.5)
    parser.add_argument("--dynamic-l3-seed", type=int, default=1)
    parser.add_argument("--dynamic-l3-base-url", default="")
    parser.add_argument("--dynamic-l3-api-key", default="")
    parser.add_argument("--dynamic-l3-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--dynamic-l4", action="store_true", help="Use call_versions L4 to optimize assistant text and tool name only.")
    parser.add_argument("--dynamic-l4-call-version", type=int, choices=[2, 3], default=2)
    parser.add_argument("--dynamic-l4-alpha", type=float, default=0.5)
    parser.add_argument("--dynamic-l4-seed", type=int, default=1)
    parser.add_argument("--dynamic-l4-base-url", default="")
    parser.add_argument("--dynamic-l4-api-key", default="")
    parser.add_argument("--dynamic-l4-timeout-seconds", type=float, default=600.0, help="Per-attempt L4 timeout; use 0 for no timeout.")
    parser.add_argument("--dynamic-l4-max-new-tokens", type=int, default=1024)
    parser.add_argument("--dynamic-l4-model", default="")
    parser.add_argument("--dynamic-l4-max-attempts", type=int, default=0, help="L4 attempts; 0 retries until the task container times out.")
    parser.add_argument("--dynamic-l4-retry-delay-seconds", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not expose_agents:
        args.agents = default_agents
    if args.dynamic_l3 and args.dynamic_l4:
        raise SystemExit("--dynamic-l3 and --dynamic-l4 are mutually exclusive")
    if args.request_max_attempts < 0:
        raise SystemExit("--request-max-attempts must be >= 0")
    if args.dynamic_l4_max_attempts < 0:
        raise SystemExit("--dynamic-l4-max-attempts must be >= 0")
    if args.dynamic_l3_backend == "call_versions_official" and args.dynamic_l3_call_version != 1:
        raise SystemExit("call_versions official L3 requires --dynamic-l3-call-version 1")
    return args


def default_run_id(
    agents: tuple[str, ...],
    injection_turns: tuple[int | None, ...],
    injection_mode: str,
    injection_turn_mode: str,
) -> str:
    agent_part = "-".join(agents)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    turns = [int(turn) for turn in injection_turns if turn is not None]
    turn_label = "random" if injection_turn_mode == "random" else ("none" if not turns else "-".join(str(turn) for turn in turns))
    return f"{agent_part}-auto-review-step{turn_label}-{injection_mode}-{timestamp}"


def validate_trace_source(args: argparse.Namespace, agents: tuple[str, ...], instance_ids: tuple[str, ...], injection_turns: tuple[int | None, ...]) -> None:
    if args.router_mode == "inject":
        if args.trace_source_root or args.trace_source_dir:
            raise SystemExit("trace sources are only valid with --router-mode replay-inject")
        return
    direct_step0_replace = args.injection_mode == "replace" and tuple(int(turn) for turn in injection_turns if turn is not None) == (0,)
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


def load_context_id_file(path: Path) -> set[str]:
    try:
        values = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError as exc:
        raise SystemExit(f"cannot read --context-id-file {path}: {exc}") from exc
    if not values:
        raise SystemExit("--context-id-file is empty")
    return values


def successfully_defended_context(run_root: Path, context_id: str) -> bool:
    context_dir = run_root / "experiments" / context_id
    result_path = context_dir / "analysis" / "result.json"
    if not result_path.exists():
        return False
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if result.get("status") != "payload_blocked":
        return False
    for injection_path in (context_dir / "router" / "traces").glob("*/injection.json"):
        try:
            injection = json.loads(injection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if injection.get("dynamic_l4", {}).get("status") == "optimized":
            return True
    return False


def context_matches_resume_status(run_root: Path, context_id: str, requested: set[str]) -> bool:
    """Select only explicit unfinished/failed contexts for a resumable matrix."""

    context_dir = run_root / "experiments" / context_id
    result_path = context_dir / "analysis" / "result.json"
    if not result_path.exists():
        return "unstarted" in requested
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unstarted" in requested
    status = str(result.get("status") or "")
    if status in requested:
        return True
    if "timeout" not in requested:
        return False
    exit_path = context_dir / "container" / "exit.json"
    try:
        exit_data = json.loads(exit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return status in {"timeout", "timed_out", "agent_timed_out"}
    return bool(exit_data.get("timed_out"))


def resolve_injection_turns(args: argparse.Namespace) -> tuple[int | None, ...]:
    if args.injection_turn_mode == "random":
        if not args.injection_steps:
            raise SystemExit("--injection-steps is required when --injection-turn-mode=random")
        if args.injection_random_seed is None:
            raise SystemExit("--injection-random-seed is required when --injection-turn-mode=random")
        return parse_turns(args.injection_steps, "replay-inject")
    if args.injection_steps:
        return parse_turns(args.injection_steps, "replay-inject")
    return (args.injection_step,)


def start_router(args: argparse.Namespace, run_root: Path, registry_path: Path) -> subprocess.Popen[bytes]:
    router_dir = run_root / "router"
    router_dir.mkdir(parents=True, exist_ok=True)
    log_path = router_dir / "uvicorn.log"
    env = os.environ.copy()
    dynamic_l3_base_url = args.dynamic_l3_base_url or env.get("DYNAMIC_L3_BASE_URL", "")
    dynamic_l3_api_key = args.dynamic_l3_api_key or env.get("DYNAMIC_L3_API_KEY", "")
    dynamic_l4_base_url = args.dynamic_l4_base_url or env.get("DYNAMIC_L4_BASE_URL", "")
    dynamic_l4_api_key = args.dynamic_l4_api_key or env.get("DYNAMIC_L4_API_KEY", "")
    env.update(
        {
            "ROUTER_CONTEXT_REGISTRY": str(registry_path),
            "ROUTER_ARTIFACT_ROOT": str(args.artifacts_root.resolve()),
            "ATTACK_DATASET_PATH": str(args.dataset.resolve()),
            "REQUEST_TIMEOUT_SECONDS": str(args.request_timeout_seconds),
            "REQUEST_MAX_ATTEMPTS": str(args.request_max_attempts),
            "ATTACK_DYNAMIC_L3_ENABLED": "1" if args.dynamic_l3 else "0",
            "DYNAMIC_L3_BACKEND": args.dynamic_l3_backend,
            "DYNAMIC_L3_CALL_VERSION": str(args.dynamic_l3_call_version),
            "DYNAMIC_L3_ALPHA": str(args.dynamic_l3_alpha),
            "DYNAMIC_L3_SEED": str(args.dynamic_l3_seed),
            "DYNAMIC_L3_BASE_URL": dynamic_l3_base_url,
            "DYNAMIC_L3_API_KEY": dynamic_l3_api_key,
            "DYNAMIC_L3_TIMEOUT_SECONDS": str(args.dynamic_l3_timeout_seconds),
            "ATTACK_DYNAMIC_L4_ENABLED": "1" if args.dynamic_l4 else "0",
            "DYNAMIC_L4_CALL_VERSION": str(args.dynamic_l4_call_version),
            "DYNAMIC_L4_ALPHA": str(args.dynamic_l4_alpha),
            "DYNAMIC_L4_SEED": str(args.dynamic_l4_seed),
            "DYNAMIC_L4_BASE_URL": dynamic_l4_base_url,
            "DYNAMIC_L4_API_KEY": dynamic_l4_api_key,
            "DYNAMIC_L4_TIMEOUT_SECONDS": str(args.dynamic_l4_timeout_seconds),
            "DYNAMIC_L4_MAX_NEW_TOKENS": str(args.dynamic_l4_max_new_tokens),
            "DYNAMIC_L4_MODEL": args.dynamic_l4_model,
            "DYNAMIC_L4_MAX_ATTEMPTS": str(args.dynamic_l4_max_attempts),
            "DYNAMIC_L4_RETRY_DELAY_SECONDS": str(args.dynamic_l4_retry_delay_seconds),
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
    health_host = "127.0.0.1" if args.router_host in {"0.0.0.0", "::"} else args.router_host
    url = f"http://{health_host}:{args.router_port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"router exited early with code {process.returncode}; see {log_path}")
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=1.0) as response:
                if response.status == 200:
                    return
        # Uvicorn can have its listening socket open while its first request is
        # still delayed by loading a large context registry.  A one-second
        # health probe then raises TimeoutError rather than URLError; treat it
        # as the same transient startup condition and keep polling.
        except (URLError, TimeoutError):
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


def run_containers(args: argparse.Namespace, run_root: Path, context_ids: set[str] | None = None) -> None:
    router_url_host = args.router_url_host or args.router_host
    orchestrator = ExperimentOrchestrator(
        run_root=run_root,
        router_url=f"http://{router_url_host}:{args.router_port}",
        tasks_path=args.tasks,
        prompt_template_path=Path("bench/prompts/solve_issue.md"),
    )
    executor = ExperimentExecutor(
        orchestrator=orchestrator,
        agent_tool_image=args.agent_tool_image,
        force_image_rebuild=bool(args.force_image_rebuild),
    )
    for spec in orchestrator.build_container_specs(context_ids=context_ids):
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
