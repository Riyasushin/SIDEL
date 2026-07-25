#!/usr/bin/env bash
# Claude + allow_all + deepseek-v4-pro 全流程编排脚本。
#
# 串行执行三个阶段：
#   Phase 1 (record)   : proxy-record + allow_all + claude，新生成干净 trace
#   Phase 2 (replace)  : replay-inject + allow_all + claude，step=3 + replace
#   Phase 3 (append)   : replay-inject + allow_all + claude，step=3 + append
#
# 全部用 sqlfluff__sqlfluff-2419 这一个 SWE-bench 任务跑全 400 条恶意样本。
# 上游模型统一从 .env 的 DEEPSEEK_MODEL 读取（默认 deepseek-v4-pro），
# DEFAULT_PROVIDER=deepseek。Phase 2/3 阶段间会强制检查容器退出、router
# 端口释放，避免残留 router 让后续 run 的容器打到错误 registry 报
# 401 invalid router api key。
#
# 常用覆盖：
#   SAMPLES=1:10 TIMEOUT_SECONDS=600 scripts/run_claude_allow_all_deepseek_full_pipeline.sh
#   DEEPSEEK_MODEL=deepseek-v4-pro INSTANCE_ID=sqlfluff__sqlfluff-2419 \
#     scripts/run_claude_allow_all_deepseek_full_pipeline.sh
#   仅做 preflight + 打印计划，不实际跑实验：
#     scripts/run_claude_allow_all_deepseek_full_pipeline.sh --dry-run
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

STAMP="$(date +%Y%m%d-%H%M%S)"
INSTANCE_ID="${INSTANCE_ID:-sqlfluff__sqlfluff-2419}"
SAMPLES="${SAMPLES:-all}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-360}"
CONTAINER_START_DELAY_SECONDS="${CONTAINER_START_DELAY_SECONDS:-1.0}"
ROUTER_HOST="${ROUTER_HOST:-127.0.0.1}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-pro}"
RECORD_PORT="${RECORD_PORT:-18080}"
REPLACE_PORT="${REPLACE_PORT:-18081}"
APPEND_PORT="${APPEND_PORT:-18082}"
RECORD_RUN_ID="${RECORD_RUN_ID:-claude-record-allow-all-${STAMP}}"
REPLACE_RUN_ID="${REPLACE_RUN_ID:-claude-step3-replace-allow-all-${STAMP}}"
APPEND_RUN_ID="${APPEND_RUN_ID:-claude-step3-append-allow-all-${STAMP}}"
POST_STEP_SLEEP_SECONDS="${POST_STEP_SLEEP_SECONDS:-15}"
RECORD_RUN_ROOT="artifacts/runs/${RECORD_RUN_ID}"
TASKS_FILE="bench/swe_lite_dev_23.jsonl"
DATASET_FILE="data/code_agent_attack_dataset.jsonl"
PROMPT_FILE="bench/prompts/solve_issue.md"

DRY_RUN=0
for arg in "$@"; do
  if [[ "${arg}" == "--dry-run" ]]; then
    DRY_RUN=1
  fi
done

# ============================================================================
# 共用 helpers
# ============================================================================
fail() {
  printf 'preflight failed: %s\n' "$1" >&2
  exit 1
}

port_is_open() {
  local port="$1"
  python3 - "${ROUTER_HOST}" "${port}" <<'PYCHECK'
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
  while port_is_open "${port}"; do
    if (( $(date +%s) - start >= deadline_seconds )); then
      echo "router port ${ROUTER_HOST}:${port} still in use after ${deadline_seconds}s" >&2
      return 1
    fi
    sleep 1
  done
}

assert_port_free() {
  local port="$1"
  if port_is_open "${port}"; then
    fail "router port ${ROUTER_HOST}:${port} is already in use"
  fi
}

running_experiment_containers() {
  docker ps --filter "label=code-agent-attack.run_id=$1" --format '{{.Names}}'
}

assert_no_run_containers() {
  local run_id="$1"
  local names
  names="$(running_experiment_containers "${run_id}")"
  if [[ -n "${names}" ]]; then
    echo "containers for ${run_id} still running; abort" >&2
    echo "${names}" >&2
    return 1
  fi
}

assert_no_stale_claude_runner() {
  local matches
  matches="$(pgrep -af 'scripts/run_claude_(auto_review_matrix|record_allow_all)\.py' || true)"
  if [[ -n "${matches}" ]]; then
    echo "stale Claude runner is still running; stop it before starting a clean serial run" >&2
    echo "${matches}" >&2
    return 1
  fi
}

docker_image_exists() {
  docker image inspect code-agent-router:local >/dev/null 2>&1
}

assert_image_present() {
  docker_image_exists || fail "Docker image code-agent-router:local is missing; run 'docker compose build'"
}

# ============================================================================
# Pre-flight checks
# ============================================================================
echo "[preflight] starting"

# 1. DEEPSEEK_API_KEY
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  if [[ ! -f .env ]] || ! grep -q '^DEEPSEEK_API_KEY=' .env; then
    fail "DEEPSEEK_API_KEY is not exported and .env does not contain DEEPSEEK_API_KEY="
  fi
fi
echo "[preflight] DEEPSEEK_API_KEY configured"

# 2. DEEPSEEK_MODEL
echo "[preflight] DEEPSEEK_MODEL=${DEEPSEEK_MODEL}"

# 3. Tools on PATH
command -v docker >/dev/null 2>&1 || fail "docker is not on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 is not on PATH"
command -v git >/dev/null 2>&1 || fail "git is not on PATH"
echo "[preflight] tools on PATH (docker, python3, git)"

# 4. Docker daemon responsive
docker info >/dev/null 2>&1 || fail "docker daemon is not responsive"
echo "[preflight] docker daemon responsive"

# 5. Docker image
assert_image_present
echo "[preflight] code-agent-router:local image present"

# 6. Manifest, dataset, prompt files
[[ -f "${TASKS_FILE}" ]] || fail "${TASKS_FILE} is missing"
TASK_COUNT="$(wc -l < "${TASKS_FILE}" | tr -d ' ')"
[[ "${TASK_COUNT}" == "23" ]] || fail "expected 23 SWE-bench Lite dev tasks, got ${TASK_COUNT}"
echo "[preflight] ${TASKS_FILE} has ${TASK_COUNT} tasks"

[[ -f "${DATASET_FILE}" ]] || fail "${DATASET_FILE} is missing"
SAMPLE_COUNT="$(wc -l < "${DATASET_FILE}" | tr -d ' ')"
[[ "${SAMPLE_COUNT}" == "400" ]] || fail "expected 400 attack samples, got ${SAMPLE_COUNT}"
echo "[preflight] ${DATASET_FILE} has ${SAMPLE_COUNT} samples"

[[ -f "${PROMPT_FILE}" ]] || fail "${PROMPT_FILE} is missing"
echo "[preflight] ${PROMPT_FILE} present"

# 7. INSTANCE_ID 必须在 manifest 里
python3 - "${INSTANCE_ID}" "${TASKS_FILE}" <<'PYCHECK' || fail "instance ${INSTANCE_ID} not in ${TASKS_FILE}"
import json
import sys
target = sys.argv[1]
path = sys.argv[2]
ids = []
with open(path, encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        ids.append(json.loads(line).get("instance_id"))
if target not in ids:
    raise SystemExit(1)
PYCHECK
echo "[preflight] task ${INSTANCE_ID} is in manifest"

# 8. Python compile + tests
python3 -m compileall router manager swebench_container >/dev/null
python3 -m unittest discover -s tests >/dev/null
echo "[preflight] compile + tests pass"

# 9. Router ports 全部空闲
assert_port_free "${RECORD_PORT}"
assert_port_free "${REPLACE_PORT}"
assert_port_free "${APPEND_PORT}"
echo "[preflight] router ports free (${RECORD_PORT}/${REPLACE_PORT}/${APPEND_PORT})"

# 10. 没有 stale runner 进程
assert_no_stale_claude_runner
echo "[preflight] no stale Claude runner"

# 11. 没有同名 run 的残留容器
assert_no_run_containers "${RECORD_RUN_ID}"
assert_no_run_containers "${REPLACE_RUN_ID}"
assert_no_run_containers "${APPEND_RUN_ID}"
echo "[preflight] no stale containers for planned run ids"

echo "[preflight] all checks passed"

if [[ "${DRY_RUN}" == "1" ]]; then
  cat <<EOF
[plan] (dry-run)
  record:  ${RECORD_RUN_ID}  port=${RECORD_PORT}
  replace: ${REPLACE_RUN_ID} port=${REPLACE_PORT}
  append:  ${APPEND_RUN_ID} port=${APPEND_PORT}
  task:    ${INSTANCE_ID}
  samples: ${SAMPLES}
  timeout: ${TIMEOUT_SECONDS}s
  model:   ${DEEPSEEK_MODEL}
  agent:   claude (allow_all = --permission-mode acceptEdits)
EOF
  exit 0
fi

# ============================================================================
# Phase 1: proxy-record 新生成干净 trace
# ============================================================================
echo ""
echo "=========================================="
echo "[phase1] record clean trace"
echo "  run_id=${RECORD_RUN_ID}"
echo "  port=${RECORD_PORT}"
echo "  agent=claude, mode=allow_all"
echo "  task=${INSTANCE_ID}"
echo "=========================================="
export DEEPSEEK_MODEL DEFAULT_PROVIDER=deepseek
python3 scripts/run_claude_record_allow_all.py \
  --run-id "${RECORD_RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --tasks "${TASKS_FILE}" \
  --dataset "${DATASET_FILE}" \
  --router-host "${ROUTER_HOST}" \
  --router-port "${RECORD_PORT}" \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  --router-start-timeout 30 \
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS}"

# 找到 record trace dir（context_id 由 planner 生成）
RECORD_TRACE_DIR=""
for dir in "${RECORD_RUN_ROOT}/experiments"/*/router/traces; do
  if [[ -d "${dir}" ]]; then
    RECORD_TRACE_DIR="${dir}"
    break
  fi
done
if [[ -z "${RECORD_TRACE_DIR}" || ! -d "${RECORD_TRACE_DIR}" ]]; then
  echo "phase1 trace dir not found under ${RECORD_RUN_ROOT}/experiments" >&2
  exit 1
fi
RESPONSE_COUNT="$(find "${RECORD_TRACE_DIR}" -name 'response.upstream.json' | wc -l | tr -d ' ')"
if [[ "${RESPONSE_COUNT}" -lt 1 ]]; then
  echo "phase1 record trace has no response.upstream.json artifacts under ${RECORD_TRACE_DIR}" >&2
  exit 1
fi
echo "[phase1] trace dir: ${RECORD_TRACE_DIR} (${RESPONSE_COUNT} response artifacts)"

# ============================================================================
# Phase 1 → 2 cleanup 守护
# ============================================================================
echo "[guard] sleeping ${POST_STEP_SLEEP_SECONDS}s before phase2 cleanup checks"
sleep "${POST_STEP_SLEEP_SECONDS}"
assert_no_run_containers "${RECORD_RUN_ID}"
wait_for_port_closed "${RECORD_PORT}" 60
assert_no_stale_claude_runner
assert_port_free "${REPLACE_PORT}"

# ============================================================================
# Phase 2: step3 replace
# ============================================================================
echo ""
echo "=========================================="
echo "[phase2] step=3 replace injection"
echo "  run_id=${REPLACE_RUN_ID}"
echo "  port=${REPLACE_PORT}"
echo "  samples=${SAMPLES}"
echo "=========================================="
python3 scripts/run_claude_auto_review_matrix.py \
  --run-id "${REPLACE_RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --injection-step 3 \
  --injection-mode replace \
  --trace-source-dir "${RECORD_TRACE_DIR}" \
  --samples "${SAMPLES}" \
  --task-limit 1 \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS}" \
  --router-host "${ROUTER_HOST}" \
  --router-port "${REPLACE_PORT}"

# ============================================================================
# Phase 2 → 3 cleanup 守护
# ============================================================================
echo "[guard] sleeping ${POST_STEP_SLEEP_SECONDS}s before phase3 cleanup checks"
sleep "${POST_STEP_SLEEP_SECONDS}"
assert_no_run_containers "${REPLACE_RUN_ID}"
wait_for_port_closed "${REPLACE_PORT}" 60
assert_no_stale_claude_runner
assert_port_free "${APPEND_PORT}"

# ============================================================================
# Phase 3: step3 append
# ============================================================================
echo ""
echo "=========================================="
echo "[phase3] step=3 append injection"
echo "  run_id=${APPEND_RUN_ID}"
echo "  port=${APPEND_PORT}"
echo "  samples=${SAMPLES}"
echo "=========================================="
python3 scripts/run_claude_auto_review_matrix.py \
  --run-id "${APPEND_RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --injection-step 3 \
  --injection-mode append \
  --trace-source-dir "${RECORD_TRACE_DIR}" \
  --samples "${SAMPLES}" \
  --task-limit 1 \
  --timeout-seconds "${TIMEOUT_SECONDS}" \
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS}" \
  --router-host "${ROUTER_HOST}" \
  --router-port "${APPEND_PORT}"

# ============================================================================
# 收尾汇总
# ============================================================================
echo ""
echo "=========================================="
echo "[done]"
echo "  record:  artifacts/runs/${RECORD_RUN_ID}"
echo "  replace: artifacts/runs/${REPLACE_RUN_ID}"
echo "  append:  artifacts/runs/${APPEND_RUN_ID}"
echo "  trace:   ${RECORD_TRACE_DIR}"
echo "=========================================="
for run_id in "${REPLACE_RUN_ID}" "${APPEND_RUN_ID}"; do
  summary="artifacts/runs/${run_id}/analysis/summary.json"
  if [[ -f "${summary}" ]]; then
    echo "[summary] ${run_id}:"
    cat "${summary}"
    echo ""
  fi
done
