#!/usr/bin/env sh
set -eu

fail() {
  printf 'preflight failed: %s\n' "$1" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail 'docker is not on PATH'
command -v python3 >/dev/null 2>&1 || fail 'python3 is not on PATH'
command -v git >/dev/null 2>&1 || fail 'git is not on PATH'

python3 - "${1:-codex}" <<'PYTHON'
import sys
from code_agents import require_agent_name
from router.config import Settings
agent = require_agent_name(sys.argv[1])
settings = Settings()
provider = "cursor" if agent == "cursor" else settings.default_provider
if not getattr(settings, f"{provider}_api_key", ""):
    raise SystemExit(f"preflight failed: {provider.upper()}_API_KEY is not configured")
PYTHON

docker image inspect code-agent-router:local >/dev/null 2>&1 || fail 'Docker image code-agent-router:local is missing; run docker compose build'
[ -f bench/swe_lite_dev_23.jsonl ] || fail 'bench/swe_lite_dev_23.jsonl is missing'
TASK_COUNT=$(wc -l < bench/swe_lite_dev_23.jsonl | tr -d ' ')
[ "$TASK_COUNT" = "23" ] || fail "expected 23 SWE-bench Lite dev tasks, got $TASK_COUNT"

python3 -m compileall router manager swebench_container >/dev/null
python3 -m unittest discover -s tests >/dev/null

printf 'preflight ok: key is configured, Docker image exists, manifest has 23 tasks, compile/tests pass\n'
