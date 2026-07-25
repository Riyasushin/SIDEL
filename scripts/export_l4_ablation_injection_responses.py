#!/usr/bin/env python3
"""Collect paired upstream and post-L4 raw responses for an alpha ablation."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


ALPHAS = ("0", "0.25", "0.5", "0.75", "1.0")
RESPONSE_FILES = (
    "response.upstream.json",
    "dynamic_l4.request.json",
    "dynamic_l4.response.raw.json",
)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def alpha_label(alpha: str) -> str:
    return f"a{alpha.replace('.', 'p')}"


def sample_id_for(manifest_path: Path, injection: dict[str, Any]) -> str:
    manifest = read_json(manifest_path)
    if isinstance(manifest, dict) and isinstance(manifest.get("dataset_sample_id"), str):
        return manifest["dataset_sample_id"]
    value = injection.get("dataset_sample_id")
    return value if isinstance(value, str) and value else manifest_path.parent.name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the raw response pair from every L4-injected turn, grouped by alpha."
    )
    parser.add_argument("--ablation-root", type=Path, required=True)
    parser.add_argument("--base-run-id", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Defaults to <ablation-root>/injection-response-pairs.",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir or args.ablation_root / "injection-response-pairs"
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        parser.error(f"output directory already exists and is non-empty: {output_dir} (pass --overwrite)")
    if output_dir.exists() and args.overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for alpha in ALPHAS:
        run_id = f"{args.base_run_id}-{alpha_label(alpha)}"
        run_root = Path("artifacts/runs") / run_id
        for injection_path in sorted(run_root.glob("experiments/*/router/traces/*/injection.json")):
            injection = read_json(injection_path)
            if not isinstance(injection, dict):
                missing.append({"alpha": alpha, "source": str(injection_path), "reason": "invalid injection.json"})
                continue
            turn_dir = injection_path.parent
            context_root = turn_dir.parents[2]
            manifest_path = context_root / "manifest.json"
            context_id = context_root.name
            sample_id = sample_id_for(manifest_path, injection)
            destination = output_dir / alpha_label(alpha) / sample_id / turn_dir.name
            destination.mkdir(parents=True, exist_ok=True)

            files: dict[str, str] = {}
            for name in RESPONSE_FILES:
                source = turn_dir / name
                if not source.is_file():
                    missing.append({"alpha": alpha, "source": str(source), "reason": "missing response artifact"})
                    continue
                target = destination / name
                shutil.copy2(source, target)
                files[name] = str(target.relative_to(output_dir))

            exported.append(
                {
                    "alpha": alpha,
                    "run_id": run_id,
                    "context_id": context_id,
                    "dataset_sample_id": sample_id,
                    "trace_turn": turn_dir.name,
                    "source_trace_dir": str(turn_dir),
                    "files": files,
                }
            )

    (output_dir / "index.json").write_text(
        json.dumps(
            {
                "description": "Every actual L4 injection turn. Each entry contains the unmodified upstream response, the L4 generate request body, and the raw L4 generate response.",
                "response_files": list(RESPONSE_FILES),
                "entries": exported,
                "missing": missing,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), "exported_turns": len(exported), "missing_files": len(missing)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
