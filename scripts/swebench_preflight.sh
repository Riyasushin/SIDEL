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
[ -f bench/swe_lite_dev_23.jsonl ] || fail 'bench/swe_lite_dev_23.jsonl is missing'
TASK_COUNT=$(wc -l < bench/swe_lite_dev_23.jsonl | tr -d ' ')
[ "$TASK_COUNT" = "23" ] || fail "expected 23 SWE-bench Lite dev tasks, got $TASK_COUNT"

python3 -m compileall router manager swebench_container >/dev/null
python3 -m unittest discover -s tests >/dev/null

printf 'preflight ok: key is configured, Docker image exists, manifest has 23 tasks, compile/tests pass\n'
