#!/usr/bin/env python3
"""Create the fixed category-stratified dataset subset for an L4 alpha ablation."""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("data/code_agent_attack_dataset.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260624)
    args = parser.parse_args()

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for line in args.dataset.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        category = str(row.get("category") or "")
        if not category:
            raise ValueError("dataset row is missing category")
        grouped[category].append(row)

    selected: list[dict[str, object]] = []
    for category in sorted(grouped):
        rows = grouped[category]
        if len(rows) < args.per_category:
            raise ValueError(f"{category} has only {len(rows)} samples")
        chooser = random.Random(f"{args.seed}:{category}")
        chosen = sorted(chooser.sample(rows, args.per_category), key=lambda row: str(row["id"]))
        selected.extend(
            {
                "id": str(row["id"]),
                "category": category,
                "subcategory": str(row.get("subcategory") or ""),
            }
            for row in chosen
        )

    payload = {
        "schema_version": 1,
        "dataset": str(args.dataset),
        "stratification": "category",
        "per_category": args.per_category,
        "seed": args.seed,
        "samples": selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(",".join(str(row["id"]) for row in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
