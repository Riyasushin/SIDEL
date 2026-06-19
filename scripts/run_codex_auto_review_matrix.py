#!/usr/bin/env python3
"""Run Codex AUTO review matrix with the shared replay-inject runner."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_injection_review_matrix import main as matrix_main


if __name__ == "__main__":
    raise SystemExit(matrix_main(default_agents="codex", expose_agents=False))
