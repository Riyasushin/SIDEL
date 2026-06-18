#!/usr/bin/env sh
set -eu

if [ -z "${KIMI_API_KEY:-}" ]; then
  if [ ! -f .env ] || ! grep -q '^KIMI_API_KEY=' .env; then
    printf '%s\n' 'KIMI_API_KEY must be exported or present in .env; do not put it in this script' >&2
    exit 1
  fi
fi

sh scripts/swebench_preflight.sh

RUN_ID_PREFIX="${RUN_ID_PREFIX:-$(date +%Y%m%d-%H%M%S-kimi-task-image-dev23)}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"
AGENTS="${AGENTS:-claude}"
EXECUTION_MODE="${EXECUTION_MODE:-swebench-task-image}"
DOCKER_IMAGE="${DOCKER_IMAGE:-code-agent-router:local}"
SWEBENCH_NAMESPACE="${SWEBENCH_NAMESPACE:-swebench}"
FORCE_TASK_IMAGE_REBUILD="${FORCE_TASK_IMAGE_REBUILD:-0}"
LIMIT_ARGS=""
REBUILD_ARGS=""

if [ "${LIMIT:-0}" != "0" ]; then
  LIMIT_ARGS="--limit ${LIMIT}"
fi

printf 'run prefix: %s\n' "$RUN_ID_PREFIX"
printf 'agents: %s\n' "$AGENTS"
printf 'timeout seconds: %s\n' "$TIMEOUT_SECONDS"

for agent in $AGENTS; do
  run_dir="artifacts/swebench-baseline/runs/${RUN_ID_PREFIX}-${agent}"
  # shellcheck disable=SC2086
  python3 scripts/swebench_run_baseline.py \
    --agent "$agent" \
    --run-id "${RUN_ID_PREFIX}-${agent}" \
    --execution-mode "$EXECUTION_MODE" \
    --docker-image "$DOCKER_IMAGE" \
    --swebench-namespace "$SWEBENCH_NAMESPACE" \
    --timeout "$TIMEOUT_SECONDS" \
    --skip-existing \
    $REBUILD_ARGS \
    $LIMIT_ARGS
  python3 scripts/swebench_summarize.py "$run_dir" --write --tail 8
  python3 scripts/swebench_report.py "$run_dir"
done
