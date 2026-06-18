#!/usr/bin/env sh
set -eu

printf "agent-review-gate-lab.sh has been retired.
" >&2
printf "The old sentinel/marker harness was removed; use scripts/swebench_attack_matrix.py and analyze review_trace_* fields from router traces.
" >&2
exit 2
