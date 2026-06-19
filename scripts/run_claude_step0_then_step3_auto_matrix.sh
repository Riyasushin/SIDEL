#!/usr/bin/env bash
set -euo pipefail

# 串行跑 Claude Code AUTO 两组实验：
# 1. step=0 + replace 覆盖注入
# 2. step=3 + append 注入
#
# 为什么不直接 sleep 后启动 step3：
# - step0 被 Ctrl-C 或异常打断时，runner/router/container 可能残留。
# - 残留 router 会让后续 run 的容器打到错误 registry，表现为 401 invalid router api key。
# - 所以这里在 step0 和 step3 之间强制检查：容器退出、runner 退出、router 端口释放。
#
# 常用覆盖参数：
#   SAMPLES=1:20 TIMEOUT_SECONDS=180 scripts/run_claude_step0_then_step3_auto_matrix.sh
#   INSTANCE_ID=sqlfluff__sqlfluff-2419 TRACE_SOURCE_DIR=... scripts/run_claude_step0_then_step3_auto_matrix.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

STAMP="$(date +%Y%m%d-%H%M%S)"
STEP0_RUN_ID="${STEP0_RUN_ID:-claude-step0-replace-auto-${STAMP}}"
STEP3_RUN_ID="${STEP3_RUN_ID:-claude-step3-append-auto-${STAMP}}"
ROUTER_HOST="${ROUTER_HOST:-127.0.0.1}"
STEP0_ROUTER_PORT="${STEP0_ROUTER_PORT:-18080}"
STEP3_ROUTER_PORT="${STEP3_ROUTER_PORT:-18081}"
POST_STEP_SLEEP_SECONDS="${POST_STEP_SLEEP_SECONDS:-20}"
SAMPLES="${SAMPLES:-all}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-360}"
CONTAINER_START_DELAY_SECONDS="${CONTAINER_START_DELAY_SECONDS:-1.0}"
INSTANCE_ID="${INSTANCE_ID:-sqlfluff__sqlfluff-2419}"
TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR:-artifacts/runs/record-sqlfluff-2419-claude/experiments/record-sqlfluff-2419-claude-sqlfluff__sqlfluff-2419-claude-auto_review-record-record/router/traces}"

port_is_open() {
  local port="$1"
  python3 - "$ROUTER_HOST" "$port" <<'PYCHECK'
import socket
import sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.socket() as sock:
    sock.settimeout(0.5)
    raise SystemExit(0 if sock.connect_ex((host, port)) == 0 else 1)
PYCHECK
}

wait_for_port_closed() {
  local port="$1"
  local deadline_seconds="${2:-60}"
  local start
  start="$(date +%s)"
  while port_is_open "$port"; do
    if (( $(date +%s) - start >= deadline_seconds )); then
      echo "router port ${ROUTER_HOST}:${port} is still in use after ${deadline_seconds}s" >&2
      return 1
    fi
    sleep 1
  done
}

assert_port_free() {
  local port="$1"
  if port_is_open "$port"; then
    echo "router port ${ROUTER_HOST}:${port} is already in use; abort" >&2
    return 1
  fi
}

running_experiment_containers() {
  docker ps --filter "label=code-agent-attack.run_id=$1" --format '{{.Names}}'
}

assert_no_run_containers() {
  local run_id="$1"
  local names
  names="$(running_experiment_containers "$run_id")"
  if [[ -n "${names}" ]]; then
    echo "containers for ${run_id} are still running; abort" >&2
    echo "${names}" >&2
    return 1
  fi
}

assert_no_stale_claude_matrix() {
  local matches
  matches="$(pgrep -af 'scripts/run_claude_auto_review_matrix.py' || true)"
  if [[ -n "${matches}" ]]; then
    echo "stale Claude matrix runner is still running; stop it before starting a clean serial run" >&2
    echo "${matches}" >&2
    return 1
  fi
}

run_step0() {
  echo "[step0] run_id=${STEP0_RUN_ID} port=${STEP0_ROUTER_PORT} samples=${SAMPLES}"
  env \
    RUN_ID="${STEP0_RUN_ID}" \
    INSTANCE_ID="${INSTANCE_ID}" \
    TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR}" \
    SAMPLES="${SAMPLES}" \
    TIMEOUT_SECONDS="${TIMEOUT_SECONDS}" \
    CONTAINER_START_DELAY_SECONDS="${CONTAINER_START_DELAY_SECONDS}" \
    scripts/run_claude_step0_replace_auto_matrix.sh \
      --router-host "${ROUTER_HOST}" \
      --router-port "${STEP0_ROUTER_PORT}"
}

run_step3() {
  echo "[step3] run_id=${STEP3_RUN_ID} port=${STEP3_ROUTER_PORT} samples=${SAMPLES}"
  env \
    RUN_ID="${STEP3_RUN_ID}" \
    INSTANCE_ID="${INSTANCE_ID}" \
    TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR}" \
    SAMPLES="${SAMPLES}" \
    TIMEOUT_SECONDS="${TIMEOUT_SECONDS}" \
    CONTAINER_START_DELAY_SECONDS="${CONTAINER_START_DELAY_SECONDS}" \
    scripts/run_claude_step3_append_auto_matrix.sh \
      --router-host "${ROUTER_HOST}" \
      --router-port "${STEP3_ROUTER_PORT}"
}

main() {
  assert_no_stale_claude_matrix
  assert_port_free "${STEP0_ROUTER_PORT}"
  assert_port_free "${STEP3_ROUTER_PORT}"

  run_step0

  echo "[guard] sleeping ${POST_STEP_SLEEP_SECONDS}s before cleanup checks"
  sleep "${POST_STEP_SLEEP_SECONDS}"
  
  assert_no_run_containers "${STEP0_RUN_ID}"
  wait_for_port_closed "${STEP0_ROUTER_PORT}" 60
  assert_no_stale_claude_matrix

  assert_port_free "${STEP3_ROUTER_PORT}"
  run_step3

  echo "[done] step0=${STEP0_RUN_ID} step3=${STEP3_RUN_ID}"
}

main "$@"
