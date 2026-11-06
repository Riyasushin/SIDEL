#!/usr/bin/env python3
"""Offline rerun of stored reviewer requests against an explicitly selected model."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException

from manager.analysis import analyze_run_with_reviewer_reruns, classify_reviewer_response
from router.config import Settings
from router.models import model_alias_to_provider
from router.protocols import anthropic_body_to_chat_completion, response_body_to_chat_completion
from router.providers.registry import build_providers



def slugify_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "reviewer-rerun"


def default_output_names(model: str) -> tuple[str, str, str, str]:
    slug = slugify_label(model)
    return (
        f"reviewer_reruns/{slug}",
        f"reviewer-rerun-{slug}-summary.json",
        f"reviewer-rerun-{slug}-details.jsonl",
        f"artifacts/reviewer-rerun-{slug}-combined-summary.json",
    )


def build_settings_for_provider(provider: str, model: str) -> Settings:
    if provider == "kimi":
        return Settings(DEFAULT_PROVIDER="kimi", KIMI_MODEL=model)
    if provider == "deepseek":
        return Settings(DEFAULT_PROVIDER="deepseek", DEEPSEEK_MODEL=model)
    raise ValueError(f"Unsupported reviewer provider: {provider}")


def selected_model(settings: Settings, provider: str, explicit_model: str = "") -> str:
    model = explicit_model or getattr(settings, f"{provider}_model", "")
    if not model:
        raise ValueError(f"An explicit model ID is required for {provider}")
    return model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay stored reviewer requests against an OpenAI-compatible upstream model.")
    parser.add_argument("run_roots", nargs="+", type=Path)
    parser.add_argument("--model", default="", help="Preferred reviewer rerun model. Falls back to provider-specific env config.")
    parser.add_argument("--provider", default="", choices=["", "deepseek", "kimi"], help="Override upstream provider. Defaults to model-based inference.")
    parser.add_argument("--omit-stop-sequences", action="store_true", help="Explicit calibration setting: remove API stop sequences, retain the original prompt, and require complete decision tags. Recorded in output; do not mix with preserved-stop runs.")
    parser.add_argument("--rerun-dirname", default="", help="Per-run rerun output directory, relative to each run root.")
    parser.add_argument("--summary-name", default="", help="Per-run summary filename written under analysis/.")
    parser.add_argument("--details-name", default="", help="Per-run details filename written under analysis/.")
    parser.add_argument("--combined-summary-path", default="", help="Combined summary JSON path for all requested runs.")
    parser.add_argument("--concurrency", type=int, default=1, help="Maximum concurrent reviewer replays per run.")
    parser.add_argument("--resume-missing-only", action="store_true", help="Only replay reviewer requests that do not yet have meta.json or error.json.")
    parser.add_argument("--retry-errors-only", action="store_true", help="Only replay reviewer requests that currently have error.json and no successful meta.json.")
    parser.add_argument("--retry-sleep-seconds", type=int, default=300, help="Sleep between retry rounds when failures remain.")
    parser.add_argument("--max-rounds", type=int, default=3, help="Finite maximum retry rounds.")
    parser.add_argument("--deadline-seconds", type=float, default=1800, help="Total wallclock deadline including queues and retry sleeps.")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def endpoint_for_reviewer_dir(reviewer_dir: Path) -> str:
    metadata = reviewer_dir / "metadata.json"
    if metadata.exists():
        payload = load_json(metadata)
        endpoint = payload.get("endpoint")
        if isinstance(endpoint, str) and endpoint:
            return endpoint
    name = reviewer_dir.name
    if name.endswith("anthropic.messages"):
        return "anthropic.messages"
    if name.endswith("openai.responses"):
        return "openai.responses"
    return "openai.chat"


def build_chat_request(
    body: dict[str, Any], *, endpoint: str, model: str,
    settings: Settings | None = None, metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if endpoint == "anthropic.messages":
        chat_body = anthropic_body_to_chat_completion(body, model=model)
    elif endpoint == "openai.responses":
        chat_body = response_body_to_chat_completion(body, model=model)
    else:
        chat_body = dict(body)
        chat_body["model"] = model
        chat_body["stream"] = False
    chat_body["stream"] = False
    return chat_body


def reviewer_stop_sequences(body: dict[str, Any]) -> tuple[str, ...]:
    value = body.get("stop_sequences", body.get("stop", []))
    if isinstance(value, str):
        return (value,)
    return tuple(item for item in (value or []) if isinstance(item, str))


def rerun_output_dir(run_root: Path, rerun_dirname: str) -> Path:
    return run_root / Path(rerun_dirname)


def iter_reviewer_dirs(run_root: Path) -> list[Path]:
    return sorted(path for path in run_root.glob("experiments/*/router/traces/*reviewer*") if path.is_dir())


def iter_failed_reviewer_dirs(run_root: Path, rerun_dirname: str) -> list[Path]:
    rerun_root = rerun_output_dir(run_root, rerun_dirname)
    reviewers: list[Path] = []
    candidates = {path.parent for name in ("error.json", "meta.json") for path in rerun_root.glob(f"*/**/{name}")}
    for rerun_dir in sorted(candidates):
        meta_path = rerun_dir / "meta.json"
        if meta_path.exists():
            meta = load_json(meta_path)
            if meta.get("status") == "ok" and meta.get("decision_status", "present") == "present":
                continue
        if not (rerun_dir / "source_ref.json").exists():
            continue
        source_ref = load_json(rerun_dir / "source_ref.json")
        source_request = source_ref.get("source_request") if isinstance(source_ref, dict) else None
        if not isinstance(source_request, str) or not source_request:
            continue
        source_path = Path(source_request)
        if source_path.name != "request.agent.json":
            continue
        reviewer_dir = source_path.parent
        if reviewer_dir.is_dir():
            reviewers.append(reviewer_dir)
    return reviewers


def iter_missing_reviewer_dirs(run_root: Path, rerun_dirname: str) -> list[Path]:
    rerun_root = rerun_output_dir(run_root, rerun_dirname)
    reviewers: list[Path] = []
    for reviewer_dir in iter_reviewer_dirs(run_root):
        rerun_dir = rerun_root / reviewer_dir.parents[2].name / reviewer_dir.name
        if (rerun_dir / "meta.json").exists() or (rerun_dir / "error.json").exists():
            continue
        reviewers.append(reviewer_dir)
    return reviewers


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def rerun_single_reviewer(
    reviewer_dir: Path,
    *,
    provider: Any,
    model: str,
    output_root: Path,
    settings: Settings | None = None,
    omit_stop_sequences: bool = False,
) -> dict[str, Any]:
    settings = settings or Settings()
    request_path = reviewer_dir / "request.upstream.json"
    if not request_path.exists():
        request_path = reviewer_dir / "request.agent.json"
    request = load_json(request_path)
    endpoint = endpoint_for_reviewer_dir(reviewer_dir)
    source_metadata = load_json(reviewer_dir / "metadata.json") if (reviewer_dir / "metadata.json").exists() else {}
    provenance = {key: source_metadata[key] for key in
                  ("reviewed_action_id", "reviewed_call_id", "review_group_id") if key in source_metadata}
    native = (endpoint == "anthropic.messages" and getattr(provider, "name", "") == "kimi"
              and settings.kimi_api_protocol == "anthropic")
    if native:
        upstream_request = provider.prepare_anthropic_body({**request, "model": model, "stream": False})
    else:
        upstream_request = build_chat_request(request, endpoint=endpoint, model=model)
        if callable(getattr(provider, "prepare_body", None)):
            upstream_request = provider.prepare_body(upstream_request)
    budget = {"original_stop_sequences": list(reviewer_stop_sequences(upstream_request))}
    if omit_stop_sequences:
        upstream_request.pop("stop", None)
        upstream_request.pop("stop_sequences", None)
    budget["request_stop_sequences"] = list(reviewer_stop_sequences(upstream_request))
    budget["stop_policy"] = "omit" if omit_stop_sequences else "preserve"
    budget["upstream_endpoint"] = "anthropic.messages" if native else "openai.chat"
    context_id = reviewer_dir.parents[2].name
    target_dir = output_root / context_id / reviewer_dir.name
    target_dir.mkdir(parents=True, exist_ok=True)
    write_json(target_dir / "request.agent.json", request)
    write_json(
        target_dir / "source_ref.json",
        {
            "context_id": context_id,
            "reviewer_dir": reviewer_dir.name,
            "source_request": str(request_path),
            "source_kind": request_path.name,
            "endpoint": endpoint,
            "model": model,
        },
    )
    # Remove stale success/error evidence before a retry; source execution
    # evidence is never edited by this script.
    for name in ("meta.json", "error.json", "response.upstream.json"):
        (target_dir / name).unlink(missing_ok=True)
    write_json(target_dir / "reviewer-budget.json", budget)
    write_json(target_dir / "request.upstream.json", upstream_request)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            response = await (provider.anthropic_message(upstream_request) if native else provider.chat_completion(upstream_request))
    except HTTPException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error = {
            "status": "error",
            "error_type": "http_exception",
            "transport_status": "error",
            **budget,
            **provenance,
            "status_code": exc.status_code,
            "detail": exc.detail,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
        }
        write_json(target_dir / "error.json", error)
        return {"success": False, "context_id": context_id, "reviewer_dir": reviewer_dir.name}
    except Exception as exc:  # pragma: no cover - defensive
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error = {
            "status": "error",
            "error_type": type(exc).__name__,
            "transport_status": "error",
            **budget,
            **provenance,
            "detail": str(exc),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
        }
        write_json(target_dir / "error.json", error)
        return {"success": False, "context_id": context_id, "reviewer_dir": reviewer_dir.name}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    write_json(target_dir / "response.upstream.json", response)
    manifest_path = reviewer_dir.parents[2] / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {}
    agent = manifest.get("agent") or ("claude" if endpoint == "anthropic.messages" else "codex")
    decision = classify_reviewer_response(agent, response)
    success = decision.outcome in {"review_allowed", "review_denied"}
    decision_status = "present" if success else "missing"
    classification = {
        "provider": getattr(provider, "name", ""),
        "normalized_decision": decision.outcome,
        **budget,
    }
    write_json(
        target_dir / "meta.json",
        {
            "status": "ok" if success else f"decision_{decision_status}",
            "transport_status": "ok",
            "decision_status": decision_status,
            **classification,
            "request_stop_sequences": list(reviewer_stop_sequences(upstream_request)),
            **provenance,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
            "endpoint": endpoint,
            "model": model,
        },
    )
    if not success:
        write_json(target_dir / "error.json", {
            "status": f"decision_{decision_status}", "transport_status": "ok", "decision_status": decision_status,
        })
    return {"success": success, "transport_success": True, "context_id": context_id, "reviewer_dir": reviewer_dir.name}


async def rerun_with_semaphore(
    semaphore: asyncio.Semaphore,
    reviewer_dir: Path,
    *,
    provider: Any,
    model: str,
    output_root: Path,
    settings: Settings | None = None,
    omit_stop_sequences: bool = False,
) -> dict[str, Any]:
    async with semaphore:
        return await rerun_single_reviewer(
            reviewer_dir,
            provider=provider,
            model=model,
            output_root=output_root,
            settings=settings,
            omit_stop_sequences=omit_stop_sequences,
        )


def validate_concurrency(value: int) -> int:
    if value <= 0:
        raise ValueError("concurrency must be positive")
    return value


async def rerun_run_root(
    run_root: Path,
    *,
    provider_name: str,
    model: str,
    rerun_dirname: str,
    summary_name: str,
    details_name: str,
    concurrency: int,
    resume_missing_only: bool,
    retry_errors_only: bool,
    omit_stop_sequences: bool = False,
) -> dict[str, Any]:
    settings = build_settings_for_provider(provider_name, model)
    provider = build_providers(settings)[provider_name]
    if retry_errors_only:
        reviewers = iter_failed_reviewer_dirs(run_root, rerun_dirname)
    elif resume_missing_only:
        reviewers = iter_missing_reviewer_dirs(run_root, rerun_dirname)
    else:
        reviewers = iter_reviewer_dirs(run_root)
    output_root = rerun_output_dir(run_root, rerun_dirname)
    started_at = datetime.now(timezone.utc).isoformat()
    semaphore = asyncio.Semaphore(validate_concurrency(concurrency))
    tasks = [
        rerun_with_semaphore(
            semaphore,
            reviewer_dir,
            provider=provider,
            model=model,
            output_root=output_root,
            settings=settings,
            omit_stop_sequences=omit_stop_sequences,
        )
        for reviewer_dir in reviewers
    ]
    async with asyncio.timeout(settings.context_timeout_seconds):
        results = await asyncio.gather(*tasks) if tasks else []
    successes = sum(1 for result in results if result["success"])
    failures = sum(1 for result in results if not result["success"])
    transport_successes = sum(1 for result in results if result.get("transport_success"))
    manifest = {
        "run_root": str(run_root),
        "provider": provider_name,
        "model": model,
        "stop_policy": "omit" if omit_stop_sequences else "preserve",
        "concurrency": concurrency,
        "reviewer_request_total": len(reviewers),
        "resume_missing_only": resume_missing_only,
        "reviewer_rerun_success": successes,
        "reviewer_rerun_failed": failures,
        "reviewer_rerun_transport_success": transport_successes,
        "retry_errors_only": retry_errors_only,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output_root / "manifest.json", manifest)
    summary = analyze_run_with_reviewer_reruns(
        run_root,
        output_root,
        summary_name=summary_name,
        details_jsonl_name=details_name,
    )
    return {
        "run_root": str(run_root),
        "rerun_root": str(output_root),
        "provider": provider_name,
        "concurrency": concurrency,
        "resume_missing_only": resume_missing_only,
        "retry_errors_only": retry_errors_only,
        "reviewer_request_total": len(reviewers),
        "reviewer_rerun_success": successes,
        "reviewer_rerun_failed": failures,
        "reviewer_rerun_transport_success": transport_successes,
        "summary": summary,
    }


async def main_async() -> int:
    args = parse_args()
    if args.max_rounds < 1 or args.deadline_seconds <= 0 or args.retry_sleep_seconds < 0:
        raise ValueError("rounds/deadline must be positive and retry sleep non-negative")
    try:
        async with asyncio.timeout(args.deadline_seconds):
            return await run_rounds(args)
    except TimeoutError:
        print("Reviewer rerun deadline exceeded", file=sys.stderr)
        return 1


async def run_rounds(args: argparse.Namespace) -> int:
    settings = Settings()
    provider_name = args.provider or model_alias_to_provider(args.model, settings.default_provider)
    model = selected_model(settings, provider_name, args.model)
    concurrency = validate_concurrency(args.concurrency)
    omit_stop_sequences = bool(getattr(args, "omit_stop_sequences", False))
    rerun_dirname, summary_name, details_name, combined_summary_path = default_output_names(model + ("-no-stop" if omit_stop_sequences else ""))
    if args.rerun_dirname:
        rerun_dirname = args.rerun_dirname
    if args.summary_name:
        summary_name = args.summary_name
    if args.details_name:
        details_name = args.details_name
    if args.combined_summary_path:
        combined_summary_path = args.combined_summary_path
    combined_path = (REPO_ROOT / combined_summary_path).resolve()
    round_index = 0
    while True:
        round_index += 1
        payloads = []
        for run_root in args.run_roots:
            payloads.append(
                await rerun_run_root(
                    run_root.resolve(),
                    provider_name=provider_name,
                    model=model,
                    rerun_dirname=rerun_dirname,
                    summary_name=summary_name,
                    details_name=details_name,
                    concurrency=concurrency,
                    resume_missing_only=args.resume_missing_only,
                    retry_errors_only=args.retry_errors_only,
                    omit_stop_sequences=omit_stop_sequences,
                )
            )
        combined = {
            "provider": provider_name,
            "model": model,
            "concurrency": concurrency,
            "resume_missing_only": args.resume_missing_only,
            "retry_errors_only": args.retry_errors_only,
            "retry_round": round_index,
            "run_count": len(payloads),
            "reviewer_request_total": sum(int(item["reviewer_request_total"]) for item in payloads),
            "reviewer_rerun_success": sum(int(item["reviewer_rerun_success"]) for item in payloads),
            "reviewer_rerun_failed": sum(int(item["reviewer_rerun_failed"]) for item in payloads),
            "reviewer_rerun_transport_success": sum(int(item.get("reviewer_rerun_transport_success", 0)) for item in payloads),
            "runs": payloads,
        }
        write_json(combined_path, combined)
        print(json.dumps({**combined, "combined_summary_path": str(combined_path)}, ensure_ascii=False, indent=2))
        if combined["reviewer_rerun_failed"] == 0:
            return 0
        if not args.retry_errors_only or round_index >= args.max_rounds:
            return 1
        await asyncio.sleep(args.retry_sleep_seconds)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
