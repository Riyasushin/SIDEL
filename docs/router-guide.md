# Router Guide

`router/` is a shared FastAPI service that mediates coding-agent traffic and records the provider/router/execution boundary.

## Endpoints

| Endpoint | Protocol |
|---|---|
| `GET /health` | Router status and context count |
| `GET /v1/models` | Advertised provider models |
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages`, `POST /messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens`, `POST /messages/count_tokens` | Anthropic token-count compatibility |

API keys may arrive through `Authorization`, `x-api-key`, `api-key`, or `openai-api-key`. Unknown keys are rejected when multiple contexts are configured.

## Request path

The three action-bearing endpoints share the same lifecycle:

1. Resolve the context.
2. Acquire the context/process request gate.
3. Allocate trace and task turns.
4. Proxy or replay.
5. Apply L1-L4 intervention at the resolved target.
6. Clamp tool timeout fields.
7. Apply optional tool defense.
8. Persist artifacts and terminal state.
9. Return JSON or protocol-correct SSE.

An exception rolls back the task-turn counter so provider retries cannot skip the configured injection turn.

## Modes

- `proxy-record` stores a clean upstream path.
- `replay` selects a recorded response.
- `replay-inject` selects a recorded response and intervenes.
- `inject` proxies live traffic and intervenes.

`should_replay_request()` is true only for replay modes. Direct step-zero replacement can create the forged response without a trace source.

## Trace and request roles

The router classifies main-agent, reviewer, bootstrap/title, token-count, and other auxiliary requests. Only main-agent traffic receives `task_turn_index`.

Each turn directory can contain:

- request, upstream, and router response payloads
- stable hashes and public context metadata
- replay lookup details
- injection before/after details
- L3/L4 request, response, validation, and retry artifacts
- defense scan and terminal markers

## Replay

Exact mode requires a stable request-hash match. Task-turn-compatible mode aligns main task turns and validates that any tool called by the recorded response is available in the live request. Incompatible responses fail closed with a structured replay outcome.

## L3

L3 renders the active task, recent trajectory, upstream response, selected attack text, and available tools into a rewrite request. The validator rejects:

- empty or wrapper-formatted output
- changed action semantics
- ungrounded trajectory prefaces
- drift in important dependency or task terms

Backend selection is `deepseek_chat` or `call_versions_official`.

## L4

L4 submits the task context, upstream answer, injection seed, generation controls, and alpha to the configured service. The parser extracts the last valid JSON object containing `assistant_text` and an optional `tool_name`.

The optimizer:

- canonicalizes tool names against the live tool list
- verifies that the tool can express the dataset arguments
- locks the final arguments to the source dataset record
- retries according to context timeout/attempt policy
- cancels work after downstream disconnection
- serializes long-running generation process-wide

## Tool defense

When enabled, `tool_defense.py` inspects action-bearing response content before delivery. It classifies suspicious command, domain, package, file, and exfiltration patterns and records benign/malicious allow/deny evidence in `defense_scan.json`.

The defense is a client-side experimental baseline, not an integrity guarantee.

## Timeout handling

Provider request timeouts and retry counts come from `REQUEST_TIMEOUT_SECONDS` and `REQUEST_MAX_ATTEMPTS`. Dynamic L3/L4 have independent limits. Before persistence and delivery, every nested tool argument named `timeout` is capped at 300 seconds.

## Starting the router

```sh
ROUTER_CONTEXT_REGISTRY=artifacts/runs/<run_id>/router/effective_config.json \
ROUTER_ARTIFACT_ROOT=artifacts/runs \
python -m uvicorn router.app:app --host 127.0.0.1 --port 18080
```

Use the generated registry for experiments. Single-context environment defaults are intended only for local smoke tests.
