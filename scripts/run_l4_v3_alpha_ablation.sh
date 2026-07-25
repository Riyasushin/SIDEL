#!/usr/bin/env bash
# Run the five fixed-subset L4-v3 alpha variants sequentially in one tmux job.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <ablation-root> <base-run-id>" >&2
  exit 2
fi

ablation_root="$1"
base_run_id="$2"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
subset_manifest="$ablation_root/subset.json"

[[ -f "$subset_manifest" ]] || { echo "missing subset manifest: $subset_manifest" >&2; exit 2; }
: "${DYNAMIC_L4_BASE_URL:?DYNAMIC_L4_BASE_URL must be set}"
: "${DYNAMIC_L4_API_KEY:?DYNAMIC_L4_API_KEY must be set}"

samples="$(python3 - "$subset_manifest" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(",".join(sample["id"] for sample in payload["samples"]))
PY
)"

export DEFAULT_PROVIDER=deepseek
export DEEPSEEK_MODEL=deepseek-v4-pro

for alpha in 0 0.25 0.5 0.75 1.0; do
  alpha_label="a${alpha//./p}"
  run_id="${base_run_id}-${alpha_label}"
  python3 "$repo_root/scripts/run_injection_review_matrix.py" \
    --run-id "$run_id" \
    --router-mode inject \
    --agents claude \
    --agent-mode auto_review \
    --task-limit 0 \
    --instance-id pydicom__pydicom-1694 \
    --samples "$samples" \
    --injection-turn-mode random \
    --injection-steps 1:10 \
    --injection-random-seed 20260624 \
    --injection-mode append \
    --router-port 18217 \
    --router-url-host host.docker.internal \
    --timeout-seconds 3600 \
    --request-timeout-seconds 600 \
    --request-max-attempts 0 \
    --network host \
    --container-start-delay-seconds 1 \
    --dynamic-l4 \
    --dynamic-l4-call-version 3 \
    --dynamic-l4-alpha "$alpha" \
    --dynamic-l4-seed 1 \
    --dynamic-l4-timeout-seconds 0 \
    --dynamic-l4-max-new-tokens 8192 \
    --dynamic-l4-model qwen3.6-27b \
    --dynamic-l4-max-attempts 0 \
    --dynamic-l4-retry-delay-seconds 120
done

python3 "$repo_root/scripts/summarize_l4_alpha_reviewer_outcomes.py" \
  --ablation-root "$ablation_root" \
  --base-run-id "$base_run_id"
