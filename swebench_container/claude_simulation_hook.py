"""Claude PreToolUse adapter: suppress only SIDEL's simulated injected IDs."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SIMULATED_CALL_PREFIX = "toolu_sidel_sim_"


def decision(event: dict[str, Any]) -> dict[str, Any] | None:
    call_id = event.get("tool_use_id")
    if isinstance(call_id, str) and not call_id.startswith(SIMULATED_CALL_PREFIX):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Tool result supplied by the experiment harness.",
    }}


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            event = {}
    except (ValueError, TypeError):
        event = {}
    result = decision(event)
    if result is None:
        return
    log = Path(os.environ.get("SIDEL_SIMULATION_LOG", "/artifacts/simulated-tool-calls.jsonl"))
    record = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "tool_use_id": event.get("tool_use_id"), "tool_name": event.get("tool_name"),
              "executed": False, "execution_mode": "simulated"}
    # A logging failure exits nonzero; the enclosing command returns exit 2,
    # which still blocks execution of this call.
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
