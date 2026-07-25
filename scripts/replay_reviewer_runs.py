#!/usr/bin/env python3
"""Offline rerun reviewer requests against a chosen DeepSeek model."""

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

from manager.analysis import analyze_run_with_reviewer_reruns
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
    return Settings(DEFAULT_PROVIDER="deepseek", DEEPSEEK_MODEL=model)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay stored reviewer requests against an OpenAI-compatible upstream model.")
    parser.add_argument("run_roots", nargs="+", type=Path)
    parser.add_argument("--model", default="", help="Preferred reviewer rerun model. Falls back to provider-specific env config.")
    parser.add_argument("--provider", default="", choices=["", "deepseek", "kimi"], help="Override upstream provider. Defaults to model-based inference.")
    parser.add_argument("--rerun-dirname", default="", help="Per-run rerun output directory, relative to each run root.")
    parser.add_argument("--summary-name", default="", help="Per-run summary filename written under analysis/.")
    parser.add_argument("--details-name", default="", help="Per-run details filename written under analysis/.")
    parser.add_argument("--combined-summary-path", default="", help="Combined summary JSON path for all requested runs.")
    parser.add_argument("--concurrency", type=int, default=1, help="Maximum concurrent reviewer replays per run.")
    parser.add_argument("--resume-missing-only", action="store_true", help="Only replay reviewer requests that do not yet have meta.json or error.json.")
    parser.add_argument("--retry-errors-only", action="store_true", help="Only replay reviewer requests that currently have error.json and no successful meta.json.")
    parser.add_argument("--retry-sleep-seconds", type=int, default=300, help="Sleep between retry rounds when --retry-errors-only is enabled and failures remain.")
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


def build_chat_request(body: dict[str, Any], *, endpoint: str, model: str) -> dict[str, Any]:
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


def rerun_output_dir(run_root: Path, rerun_dirname: str) -> Path:
    return run_root / Path(rerun_dirname)


def iter_reviewer_dirs(run_root: Path) -> list[Path]:
    return sorted(path for path in run_root.glob("experiments/*/router/traces/*reviewer*") if path.is_dir())


def iter_failed_reviewer_dirs(run_root: Path, rerun_dirname: str) -> list[Path]:
    rerun_root = rerun_output_dir(run_root, rerun_dirname)
    reviewers: list[Path] = []
    for error_path in sorted(rerun_root.glob("*/**/error.json")):
        rerun_dir = error_path.parent
        if (rerun_dir / "meta.json").exists():
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
) -> dict[str, Any]:
    request = load_json(reviewer_dir / "request.agent.json")
    endpoint = endpoint_for_reviewer_dir(reviewer_dir)
    chat_request = build_chat_request(request, endpoint=endpoint, model=model)
    context_id = reviewer_dir.parents[2].name
    target_dir = output_root / context_id / reviewer_dir.name
    target_dir.mkdir(parents=True, exist_ok=True)
    write_json(target_dir / "request.agent.json", request)
    write_json(
        target_dir / "source_ref.json",
        {
            "context_id": context_id,
            "reviewer_dir": reviewer_dir.name,
            "source_request": str(reviewer_dir / "request.agent.json"),
            "endpoint": endpoint,
            "model": model,
        },
    )
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        response = await provider.chat_completion(chat_request)
    except HTTPException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error = {
            "status": "error",
            "error_type": "http_exception",
            "status_code": exc.status_code,
            "detail": exc.detail,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
        }
        write_json(target_dir / "request.upstream.json", chat_request)
        write_json(target_dir / "error.json", error)
        return {"success": False, "context_id": context_id, "reviewer_dir": reviewer_dir.name}
    except Exception as exc:  # pragma: no cover - defensive
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error = {
            "status": "error",
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
        }
        write_json(target_dir / "request.upstream.json", chat_request)
        write_json(target_dir / "error.json", error)
        return {"success": False, "context_id": context_id, "reviewer_dir": reviewer_dir.name}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    write_json(target_dir / "request.upstream.json", chat_request)
    write_json(target_dir / "response.upstream.json", response)
    write_json(
        target_dir / "meta.json",
        {
            "status": "ok",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
            "endpoint": endpoint,
            "model": model,
        },
    )
    error_path = target_dir / "error.json"
    if error_path.exists():
        error_path.unlink()
    return {"success": True, "context_id": context_id, "reviewer_dir": reviewer_dir.name}


async def rerun_with_semaphore(
    semaphore: asyncio.Semaphore,
    reviewer_dir: Path,
    *,
    provider: Any,
    model: str,
    output_root: Path,
) -> dict[str, Any]:
    async with semaphore:
        return await rerun_single_reviewer(
            reviewer_dir,
            provider=provider,
            model=model,
            output_root=output_root,
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
        )
        for reviewer_dir in reviewers
    ]
    results = await asyncio.gather(*tasks) if tasks else []
    successes = sum(1 for result in results if result["success"])
    failures = sum(1 for result in results if not result["success"])
    manifest = {
        "run_root": str(run_root),
        "provider": provider_name,
        "model": model,
        "concurrency": concurrency,
        "reviewer_request_total": len(reviewers),
        "resume_missing_only": resume_missing_only,
        "reviewer_rerun_success": successes,
        "reviewer_rerun_failed": failures,
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
        "summary": summary,
    }


async def main_async() -> int:
    args = parse_args()
    settings = Settings()
    model = args.model or settings.kimi_model or settings.deepseek_model
    provider_name = args.provider or model_alias_to_provider(model, settings.default_provider)
    concurrency = validate_concurrency(args.concurrency)
    rerun_dirname, summary_name, details_name, combined_summary_path = default_output_names(model)
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
            "runs": payloads,
        }
        write_json(combined_path, combined)
        print(json.dumps({**combined, "combined_summary_path": str(combined_path)}, ensure_ascii=False, indent=2))
        if not args.retry_errors_only or combined["reviewer_rerun_failed"] == 0:
            return 0
        await asyncio.sleep(args.retry_sleep_seconds)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
