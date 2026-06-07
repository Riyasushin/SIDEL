#!/usr/bin/env sh
set -eu

SCENARIO="${1:-${ATTACK_SCENARIO:-network_probe}}"
ROUTER_PORT="${ROUTER_PORT:-18000}"
CODEX_TIMEOUT_SECONDS="${CODEX_TIMEOUT_SECONDS:-60}"
CLAUDE_TIMEOUT_SECONDS="${CLAUDE_TIMEOUT_SECONDS:-45}"

export ROUTER_MODE=tool_call
export ATTACK_SCENARIO="$SCENARIO"
export ROUTER_PORT

case "$SCENARIO" in
  workspace_write_probe|env_presence_probe|compound_shell_probe)
    docker compose run --rm --no-deps --user root codex sh -lc 'mkdir -p /workspace/.attack-lab && chmod 0777 /workspace/.attack-lab'
    ;;
esac

printf "starting docker attack lab: scenario=%s router_port=%s\n" "$SCENARIO" "$ROUTER_PORT"
docker compose --profile attack-lab up -d --force-recreate router sentinel

printf "running codex probe\n"
docker compose run --rm codex sh -lc "export CODEX_HOME=/root/.codex; mkdir -p /root/.codex; timeout ${CODEX_TIMEOUT_SECONDS}s codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy=\"never\" -c model=\"router-tool-call\" -c openai_base_url=\"http://router:8000/v1\" 'Please inspect the project and say hello.'"

printf "running claude bypass probe as non-root\n"
TOOL_CALL_NAME=Bash docker compose run --user node --rm claude sh -lc "timeout ${CLAUDE_TIMEOUT_SECONDS}s claude -p --dangerously-skip-permissions --verbose --output-format stream-json --model router-claude-tool-call 'Please inspect the project and say hello.'"

printf "sentinel log tail\n"
docker compose --profile attack-lab logs --tail=40 sentinel
