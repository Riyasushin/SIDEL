#!/usr/bin/env bash
set -euo pipefail

# Claude Code AUTO 模式，全数据集 step=3 + append 注入。
# append 需要 clean replay trace；单任务推荐用 TRACE_SOURCE_DIR 指向已有 record 的 router/traces。
# 默认 TRACE_SOURCE_DIR 是当前仓库里已有的 sqlfluff__sqlfluff-2419 Claude record trace。
RUN_ID="${RUN_ID:-claude-step3-append-auto-$(date +%Y%m%d-%H%M%S)}"
INSTANCE_ID="${INSTANCE_ID:-sqlfluff__sqlfluff-2419}"
TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR:-artifacts/runs/record-sqlfluff-2419-claude/experiments/record-sqlfluff-2419-claude-sqlfluff__sqlfluff-2419-claude-auto_review-record-record/router/traces}"

python3 scripts/run_claude_auto_review_matrix.py \
  --run-id "${RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --injection-step 3 \
  --injection-mode append \
  --trace-source-dir "${TRACE_SOURCE_DIR}" \
  --samples "${SAMPLES:-all}" \
  --task-limit "${TASK_LIMIT:-1}" \
  --timeout-seconds "${TIMEOUT_SECONDS:-360}" \
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS:-1.0}" \
  "$@"
