#!/usr/bin/env python3
"""Run Claude Code auto-review over attack dataset items.

Pipeline used by this script:
1. Plan contexts with manager.planner:
   - agent = claude only
   - agent_mode = auto_review, mapped by ClaudeAdapter to
     `claude --permission-mode auto --print ...`
   - router_mode = replay-inject
   - injection_step is controlled by --injection-step
   - injection_mode is controlled by --injection-mode
   - payload_execution_allowed = true, so dataset tool_arguments render as raw
2. Start the shared router locally with:
   - ROUTER_CONTEXT_REGISTRY=<artifacts-root>/<run-id>/router/effective_config.json
   - ROUTER_ARTIFACT_ROOT=<artifacts-root>
   - ATTACK_DATASET_PATH=<dataset>
3. Run each planned SWE-bench container sequentially through Docker. The
   DockerTaskRunner monitors this context's router/traces/*/terminal.json and
   force-removes only the matching container after the router terminal-stops
   the main agent with 409.
4. Record Claude reviewer requests separately from main-agent turns.
5. Run manager.analysis.analyze_run() to summarize reviewer and execution
   evidence.

Recommended entry points:
- scripts/run_claude_step0_replace_auto_matrix.sh
- scripts/run_claude_step3_append_auto_matrix.sh

Important parameters:
- --injection-step: 0 for first response, 3 for replaying three main turns first
- --injection-mode: replace for full response override, append for replay append
- --trace-source-dir: required for single-task step3 append unless using root layout
- --samples: defaults to all dataset rows; can be 1:10 or a sample id
- --container-start-delay-seconds: keep >0 on weak CPU machines
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_injection_review_matrix import main as matrix_main


if __name__ == "__main__":
    raise SystemExit(matrix_main(default_agents="claude", expose_agents=False))
