#!/usr/bin/env bash
set -euo pipefail

# Claude Code AUTO 模式，全数据集 step=0 + replace 覆盖注入。
# 这里的 step=0 指第一条真正携带用户任务 prompt 的主请求；Claude 的 title/bootstrap 请求不计入 step。
# 输出默认写到 artifacts/runs/<RUN_ID>；RUN_ID 不传时带时间戳，避免旧结果污染统计。
RUN_ID="${RUN_ID:-claude-step0-replace-auto-$(date +%Y%m%d-%H%M%S)}"
INSTANCE_ID="${INSTANCE_ID:-sqlfluff__sqlfluff-2419}"
TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR:-artifacts/runs/record-sqlfluff-2419-claude/experiments/record-sqlfluff-2419-claude-sqlfluff__sqlfluff-2419-claude-auto_review-record-record/router/traces}"
TRACE_SOURCE_ARGS=()
if [[ -n "${TRACE_SOURCE_DIR}" ]]; then
  TRACE_SOURCE_ARGS=(--trace-source-dir "${TRACE_SOURCE_DIR}")
fi

python3 scripts/run_claude_auto_review_matrix.py \
  --run-id "${RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --injection-step 0 \
  --injection-mode replace \
  --samples "${SAMPLES:-all}" \
  --task-limit "${TASK_LIMIT:-1}" \
  --timeout-seconds "${TIMEOUT_SECONDS:-360}" \
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS:-1.0}" \
  "${TRACE_SOURCE_ARGS[@]}" \
  "$@"
