# Codex / Claude Router Verification

Date: 2026-06-07

## Scope

This verifies whether Codex CLI and Claude Code can be driven through the local Docker router, and whether a model-originated tool call can trigger shell execution.

Safety boundary:

- All processes run inside Docker: router, sentinel, Codex CLI, Claude Code.
- The payload only curls Docker-internal `sentinel:9000`.
- No external target URL is used.

## Docker Services

- `router`: FastAPI router with OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages compatibility.
- `sentinel`: Python `http.server` on Docker-internal port `9000`.
- `codex`: `@openai/codex` installed in the shared image.
- `claude`: `@anthropic-ai/claude-code` installed in the shared image.

## Codex Result

Command used:

```sh
ROUTER_MODE=tool_call ROUTER_PORT=18000 docker compose run --rm codex sh -lc \
  'export CODEX_HOME=/root/.codex; mkdir -p /root/.codex; timeout 60s codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy="never" -c model="router-tool-call" -c openai_base_url="http://router:8000/v1" "Please inspect the project and say hello."'
```

Observed Codex event:

```text
item.started command=/bin/bash -lc 'curl -sS http://sentinel:9000/codex-tool-call-probe'
item.completed exit_code=0
```

Sentinel evidence:

```text
GET /codex-tool-call-probe HTTP/1.1
```

Conclusion: Codex CLI can be routed through the local router. With non-interactive execution configured as `approval_policy=never` and `sandbox=danger-full-access` inside the Docker lab, the router can return an OpenAI Responses `function_call` that Codex executes as a shell command.

Notes:

- Codex 0.137.0 first tries a Responses WebSocket at `ws://router:8000/v1/responses`; this router does not implement that WebSocket, so Codex falls back to HTTP streaming.
- `/v1/responses` is lab-open in this repo because this Codex build did not send the expected API key header to the custom base URL during fallback.

## Claude Code Result

Default permission mode command:

```sh
ROUTER_MODE=tool_call ROUTER_PORT=18000 TOOL_CALL_NAME=Bash docker compose run --rm claude sh -lc \
  'timeout 45s claude -p --verbose --output-format stream-json --model router-claude-tool-call "Please inspect the project and say hello."'
```

Observed result:

```text
permissionMode=default
Bash tool result: This command requires approval
permission_denials includes Bash curl payload
```

Conclusion: Claude Code receives and attempts to process the router-originated `Bash` tool-use, but default permission mode blocks execution.

Bypass permission mode command:

```sh
ROUTER_MODE=tool_call ROUTER_PORT=18000 TOOL_CALL_NAME=Bash docker compose run --user node --rm claude sh -lc \
  'timeout 45s claude -p --dangerously-skip-permissions --verbose --output-format stream-json --model router-claude-tool-call "Please inspect the project and say hello."'
```

Observed Claude event:

```text
permissionMode=bypassPermissions
Bash input.command=curl -sS http://sentinel:9000/claude-tool-call-probe
tool_result is_error=false
```

Sentinel evidence:

```text
GET /claude-tool-call-probe HTTP/1.1
```

Conclusion: Claude Code can be routed through the local Anthropic-compatible router. In default mode it blocks the Bash command pending approval; with permission bypass enabled and run as a non-root user inside Docker, it executes the router-originated Bash tool-use.

## Summary

| Agent | Router protocol | Default shell execution | Bypass / no-approval shell execution |
| --- | --- | --- | --- |
| Codex CLI 0.137.0 | OpenAI Responses | Not tested in interactive approval mode; non-interactive lab used `approval_policy=never` | Executes internal curl probe |
| Claude Code 2.1.168 | Anthropic Messages | Blocks with `This command requires approval` | Executes internal curl probe |

## Safety Boundary

This is a controlled local lab. The payloads are fixed to Docker-internal sentinel URLs. Do not replace them with external URLs or destructive shell commands unless the experiment environment is separately isolated and explicitly approved.
