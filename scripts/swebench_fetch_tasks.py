#!/usr/bin/env python3
"""Fetch the SWE-bench Lite dev split task snapshot.

Uses the Hugging Face datasets-server rows API to avoid adding a runtime dependency on
`datasets`. The output is a JSONL file checked into the repo as a reproducible task
manifest; it does not contain secrets.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SPLIT = "dev"
DEFAULT_OUTPUT = Path("bench/swe_lite_dev_23.jsonl")


def fetch_rows(dataset: str, split: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "dataset": dataset,
            "config": "default",
            "split": split,
            "offset": 0,
            "length": 100,
        }
    )
    url = f"https://datasets-server.huggingface.co/rows?{query}"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("rows", [])
    return [item["row"] for item in rows if isinstance(item, dict) and isinstance(item.get("row"), dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = fetch_rows(args.dataset, args.split)
    if len(rows) != 23:
        raise SystemExit(f"expected 23 SWE-bench Lite dev rows, got {len(rows)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {len(rows)} tasks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
