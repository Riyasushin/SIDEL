#!/usr/bin/env sh
set -eu

fail() {
  printf 'preflight failed: %s\n' "$1" >&2
  exit 1
}

if [ -z "${KIMI_API_KEY:-}" ]; then
  if [ ! -f .env ] || ! grep -q '^KIMI_API_KEY=' .env; then
    fail 'KIMI_API_KEY is not exported and .env does not contain KIMI_API_KEY='
  fi
fi
command -v docker >/dev/null 2>&1 || fail 'docker is not on PATH'
command -v python3 >/dev/null 2>&1 || fail 'python3 is not on PATH'
command -v git >/dev/null 2>&1 || fail 'git is not on PATH'

docker image inspect code-agent-router:local >/dev/null 2>&1 || fail 'Docker image code-agent-router:local is missing; run docker compose build'
if [ "${KIMI_MODEL:-kimi-for-coding}" != "kimi-for-coding" ]; then
  fail 'Kimi Code Plan runs must use KIMI_MODEL=kimi-for-coding'
fi
if [ "${KIMI_BASE_URL:-https://api.kimi.com/coding/v1}" != "https://api.kimi.com/coding/v1" ]; then
  fail 'Kimi Code Plan OpenAI-compatible endpoint must be https://api.kimi.com/coding/v1'
fi
if [ "${KIMI_ANTHROPIC_BASE_URL:-https://api.kimi.com/coding}" != "https://api.kimi.com/coding" ]; then
  fail 'Kimi Code Plan Anthropic-compatible endpoint must be https://api.kimi.com/coding'
fi
[ -f bench/swe_lite_dev_23.jsonl ] || fail 'bench/swe_lite_dev_23.jsonl is missing'
TASK_COUNT=$(wc -l < bench/swe_lite_dev_23.jsonl | tr -d ' ')
[ "$TASK_COUNT" = "23" ] || fail "expected 23 SWE-bench Lite dev tasks, got $TASK_COUNT"

python3 -m py_compile scripts/swebench_run_baseline.py router/app.py router/providers/openai_compatible.py >/dev/null

if docker network ls --format '{{.Name}}' | grep -q '^swebench-'; then
  printf 'warning: existing swebench-* Docker networks found; runner normally cleans its own resources\n' >&2
fi
if docker ps -a --format '{{.Names}}' | grep -q '^swebench-router-'; then
  printf 'warning: existing swebench-router-* containers found; runner normally cleans its own resources\n' >&2
fi

printf 'preflight ok: KIMI_API_KEY is available via environment or .env, Docker image exists, manifest has 23 tasks\n'
