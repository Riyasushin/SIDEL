"""Command line entrypoint for experiment planning, execution, and analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from manager.analysis import analyze_run
from manager.dataset_profile import write_dataset_profile
from manager.executor import ExperimentExecutor
from manager.manifest import AgentMode, InjectionMode, InjectionTurnMode, RouterMode, write_manifests
from manager.orchestrator import ExperimentOrchestrator
from manager.workflow import add_run_arguments, run
from manager.planner import (
    MatrixPlanOptions,
    build_matrix_plan,
    load_dataset_sample_ids,
    load_task_instance_ids,
    parse_agents,
    parse_turns,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m manager.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    workflow_parser = subparsers.add_parser("run", help="plan, start router, run containers, and analyze")
    add_run_arguments(workflow_parser)
    plan_parser = subparsers.add_parser("plan", help="write context manifests and shared router config")
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/runs"))
    plan_parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    plan_parser.add_argument("--task-limit", type=int, default=0)
    plan_parser.add_argument("--instance-id", action="append", default=[])
    plan_parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    plan_parser.add_argument("--samples", default="1")
    plan_parser.add_argument("--agents", default="codex,claude")
    plan_parser.add_argument("--agent-mode", default=None, help="Native mode for the selected agents; omit for per-agent defaults")
    plan_parser.add_argument("--router-mode", choices=["proxy-record", "replay", "replay-inject", "inject"], default="proxy-record")
    plan_parser.add_argument("--injection-turns", default="0")
    plan_parser.add_argument("--injection-turn-mode", choices=["fixed", "random"], default="fixed")
    plan_parser.add_argument("--injection-random-seed", type=int, default=None)
    plan_parser.add_argument("--injection-mode", choices=["append", "replace"], default="append")
    plan_parser.add_argument("--payload-execution-allowed", action="store_true")
    plan_parser.add_argument("--enable-tool-defense", action="store_true")
    plan_parser.add_argument("--dynamic-l3", action="store_true")
    plan_parser.add_argument("--dynamic-l4", action="store_true")
    plan_parser.add_argument("--timeout-seconds", type=int, default=1800)
    plan_parser.add_argument("--trace-source-root", type=Path, default=None)
    plan_parser.add_argument("--replay-match-mode", choices=["exact_request_hash", "task_turn_compatible"], default="exact_request_hash")
    analyze_parser = subparsers.add_parser("analyze", help="summarize context result files under a run root")
    analyze_parser.add_argument("run_root", type=Path)

    profile_parser = subparsers.add_parser("profile-dataset", help="write dataset profile JSON")
    profile_parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    profile_parser.add_argument("--output", type=Path, required=True)

    specs_parser = subparsers.add_parser("container-specs", help="render planned container specs without running Docker")
    specs_parser.add_argument("run_root", type=Path)
    specs_parser.add_argument("--router-url", default="http://127.0.0.1:8000")
    specs_parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    specs_parser.add_argument("--prompt-template", type=Path, default=Path("bench/prompts/solve_issue.md"))

    run_parser = subparsers.add_parser("run-containers", help="run planned containers sequentially")
    run_parser.add_argument("run_root", type=Path)
    run_parser.add_argument("--router-url", default="http://127.0.0.1:8000")
    run_parser.add_argument("--tasks", type=Path, default=Path("bench/swe_lite_dev_23.jsonl"))
    run_parser.add_argument("--prompt-template", type=Path, default=Path("bench/prompts/solve_issue.md"))
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--build-images", action="store_true")
    run_parser.add_argument("--workers", type=positive_int, default=4)
    run_parser.add_argument("--agent-tool-image", default="code-agent-router:local")
    run_parser.add_argument("--force-image-rebuild", action="store_true")

    args = parser.parse_args()

    if args.command == "run":
        return run(args)
    if args.command == "plan":
        return plan(args)
    if args.command == "analyze":
        print(json.dumps(analyze_run(args.run_root), ensure_ascii=False))
        return 0
    if args.command == "profile-dataset":
        write_dataset_profile(args.dataset, args.output)
        print(json.dumps({"output": str(args.output)}, ensure_ascii=False))
        return 0
    if args.command == "container-specs":
        orchestrator = build_orchestrator(args)
        specs = orchestrator.build_container_specs()
        print(json.dumps({"contexts": [spec.context_id for spec in specs], "count": len(specs)}, ensure_ascii=False))
        return 0
    if args.command == "run-containers":
        executor = ExperimentExecutor(
            orchestrator=build_orchestrator(args),
            agent_tool_image=args.agent_tool_image,
            force_image_rebuild=bool(args.force_image_rebuild),
        )
        results = executor.run_all(dry_run=bool(args.dry_run), build_images=bool(args.build_images), workers=args.workers)
        print(json.dumps({"contexts": [result.context_id for result in results], "count": len(results)}, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)


def build_orchestrator(args: argparse.Namespace) -> ExperimentOrchestrator:
    return ExperimentOrchestrator(
        run_root=args.run_root,
        router_url=args.router_url,
        tasks_path=args.tasks,
        prompt_template_path=args.prompt_template,
    )


def plan(args: argparse.Namespace) -> int:
    router_mode: RouterMode = args.router_mode
    injection_turn_mode: InjectionTurnMode = args.injection_turn_mode
    injection_turns = parse_turns(args.injection_turns, router_mode)
    direct_replace = (
        router_mode == "replay-inject"
        and args.injection_mode == "replace"
        and injection_turn_mode == "fixed"
        and injection_turns == (0,)
    )
    if router_mode == "replay" and args.trace_source_root is None:
        raise SystemExit("--trace-source-root is required for replay")
    if router_mode == "replay-inject" and not direct_replace and args.trace_source_root is None:
        raise SystemExit("--trace-source-root is required for replay-inject unless using fixed turn 0 with --injection-mode=replace")
    if injection_turn_mode == "random":
        if router_mode not in {"replay-inject", "inject"}:
            raise SystemExit("--injection-turn-mode=random is only supported for replay-inject and inject")
        if args.injection_random_seed is None:
            raise SystemExit("--injection-random-seed is required when --injection-turn-mode=random")
    if args.dynamic_l3 and args.dynamic_l4:
        raise SystemExit("--dynamic-l3 and --dynamic-l4 are mutually exclusive")
    options = MatrixPlanOptions(
        run_id=args.run_id,
        router_mode=router_mode,
        agents=parse_agents(args.agents),
        agent_mode=args.agent_mode,  # type: ignore[arg-type]
        instance_ids=load_task_instance_ids(args.tasks, limit=args.task_limit, selected=args.instance_id),
        dataset_sample_ids=load_dataset_sample_ids(args.dataset, args.samples),
        injection_turns=injection_turns,
        injection_mode=args.injection_mode,  # type: ignore[arg-type]
        payload_execution_allowed=bool(args.payload_execution_allowed),
        enable_tool_defense=bool(args.enable_tool_defense),
        dynamic_l3_enabled=bool(args.dynamic_l3),
        dynamic_l4_enabled=bool(args.dynamic_l4),
        timeout_seconds=args.timeout_seconds,
        trace_source_root=args.trace_source_root,
        replay_match_mode=args.replay_match_mode,
        injection_turn_mode=injection_turn_mode,
        injection_random_seed=args.injection_random_seed,
    )
    keyed_manifests = build_matrix_plan(options)
    registry_path = write_manifests(args.artifacts_root, keyed_manifests, router_mode)
    print(json.dumps({"registry_path": str(registry_path), "contexts": len(keyed_manifests)}, ensure_ascii=False))
    return 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
