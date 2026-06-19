#!/usr/bin/env bash
set -euo pipefail

# Codex AUTO matrix runner. Defaults to step0 replace, then step3 append.
# Override with, for example:
#   SAMPLES=1:20 STEP_N=24 TIMEOUT_SECONDS=360 scripts/run_codex_step0_then_stepn_auto_matrix.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

STAMP="$(date +%Y%m%d-%H%M%S)"
INSTANCE_ID="${INSTANCE_ID:-sqlfluff__sqlfluff-2419}"
SAMPLES="${SAMPLES:-all}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-360}"
CONTAINER_START_DELAY_SECONDS="${CONTAINER_START_DELAY_SECONDS:-1.0}"
ROUTER_HOST="${ROUTER_HOST:-127.0.0.1}"
STEP0_ROUTER_PORT="${STEP0_ROUTER_PORT:-18090}"
STEPN_ROUTER_PORT="${STEPN_ROUTER_PORT:-18091}"
STEP_N="${STEP_N:-3}"
STEP0_RUN_ID="${STEP0_RUN_ID:-codex-step0-replace-auto-${STAMP}}"
STEPN_RUN_ID="${STEPN_RUN_ID:-codex-step${STEP_N}-append-auto-${STAMP}}"
TRACE_SOURCE_DIR="${TRACE_SOURCE_DIR:-artifacts/runs/record-sqlfluff-2419-codex/experiments/record-sqlfluff-2419-codex-sqlfluff__sqlfluff-2419-codex-auto_review-record-record/router/traces}"

common_args=(
  --instance-id "${INSTANCE_ID}"
  --samples "${SAMPLES}"
  --timeout-seconds "${TIMEOUT_SECONDS}"
  --container-start-delay-seconds "${CONTAINER_START_DELAY_SECONDS}"
  --router-host "${ROUTER_HOST}"
)

if [[ "${RUN_STEP0:-1}" == "1" ]]; then
  echo "[codex step0] run_id=${STEP0_RUN_ID} port=${STEP0_ROUTER_PORT} samples=${SAMPLES}"
  python3 scripts/run_codex_auto_review_matrix.py \
    "${common_args[@]}" \
    --run-id "${STEP0_RUN_ID}" \
    --injection-step 0 \
    --injection-mode replace \
    --router-port "${STEP0_ROUTER_PORT}"
fi

if [[ "${RUN_STEPN:-1}" == "1" ]]; then
  python3 - "${TRACE_SOURCE_DIR}" <<'PYCHECK'
import sys
from pathlib import Path

trace_dir = Path(sys.argv[1])
if not trace_dir.exists():
    raise SystemExit(f"[codex stepN] trace source does not exist: {trace_dir}")

response_names = {
    "response.router.after_injection.json",
    "response.router.after.json",
    "response.router.before_injection.json",
    "response.router.before.json",
}
has_response = any(
    child.is_file() and child.name in response_names
    for turn_dir in trace_dir.iterdir()
    if turn_dir.is_dir()
    for child in turn_dir.iterdir()
)
if not has_response:
    raise SystemExit(
        "[codex stepN] BAD trace source: no replay response artifacts found under "
        f"{trace_dir}. Re-record a complete Codex proxy-record trace before rerunning stepN."
    )
PYCHECK
  echo "[codex step${STEP_N}] run_id=${STEPN_RUN_ID} port=${STEPN_ROUTER_PORT} samples=${SAMPLES} trace=${TRACE_SOURCE_DIR}"
  python3 scripts/run_codex_auto_review_matrix.py \
    "${common_args[@]}" \
    --run-id "${STEPN_RUN_ID}" \
    --injection-step "${STEP_N}" \
    --injection-mode append \
    --trace-source-dir "${TRACE_SOURCE_DIR}" \
    --router-port "${STEPN_ROUTER_PORT}"
fi

echo "[done] step0=${STEP0_RUN_ID} step${STEP_N}=${STEPN_RUN_ID}"
