"""Dataset profiling for experiment reports."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def profile_dataset(dataset_path: Path) -> dict[str, Any]:
    rows = load_dataset(dataset_path)
    return {
        "total": len(rows),
        "by_category": dict(Counter(str(row.get("category") or "unknown") for row in rows)),
        "by_tool_name": dict(Counter(str(row.get("tool_name") or "unknown") for row in rows)),
        "by_severity": dict(Counter(str(row.get("severity") or "unknown") for row in rows)),
        "requires_shell": count_bool(rows, "requires_shell"),
        "requires_network": count_bool(rows, "requires_network"),
        "requires_file_write": count_bool(rows, "requires_file_write"),
        "has_observable_signal": count_bool(rows, "has_observable_signal"),
    }


def write_dataset_profile(dataset_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile_dataset(dataset_path), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_dataset(dataset_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    if not rows:
        raise ValueError(f"empty dataset: {dataset_path}")
    return rows


def count_bool(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter = Counter("true" if bool(row.get(key)) else "false" for row in rows)
    return dict(counter)
