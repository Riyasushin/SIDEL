# SIDEL Architecture

## System boundary

SIDEL measures the gap between the response produced by an upstream model and the repository-level action executed by a coding agent after the response passes through a third-party router.

```text
manager
  ├─ plans agent × task × sample × turn contexts
  ├─ writes manifests and the API-key context registry
  ├─ launches isolated task containers
  └─ analyzes review and execution evidence
          |
          v
shared FastAPI router
  ├─ OpenAI Chat Completions
  ├─ OpenAI Responses
  ├─ Anthropic Messages
  ├─ proxy-record / replay / replay-inject / inject
  ├─ L1-L4 response intervention
  ├─ optional tool-call defense
  └─ context-scoped traces and terminal state
          ^
          |
official SWE-bench task container
  └─ Codex, Claude Code, OpenCode, or Cursor operating in /testbed
```

The manager controls experiment dimensions. The router controls the response path. The container harness owns repository execution. Dataset IDs, injection turns, and router modes never appear in the SWE-bench task prompt.

## Modules

### `manager/`

The manager creates `ExperimentManifest` records, generates unique routing keys, writes `effective_config.json`, resolves task images, schedules containers, and classifies results. It owns the experimental matrix but does not process LLM traffic.

`cli.py run` delegates to `workflow.py` for plan validation, context selection,
router startup, sequential container execution, and analysis. `router_process.py`
starts Uvicorn with the active Python interpreter and provides readiness and
shutdown helpers. Explicit L3/L4 service options enter the child environment;
unset options use the router settings from the environment or `.env`. Startup
failures and execution exceptions terminate the router child.

### `router/`

The router exposes OpenAI- and Anthropic-compatible endpoints, plus a separate `/cursor/` native RPC recording proxy. The API key resolves to one immutable `ExperimentContext`, which contains the agent, task, mode, trace source, injection policy, and defense settings.

Important components:

- `app.py`: HTTP wiring and mode dispatch.
- `context.py`: API-key routing and context parsing.
- `request_gate.py`: per-context locking plus process-wide single-flight protection for long dynamic generation.
- `request_lifecycle.py`: trace start/finish, terminal checks, defense application, and timeout normalization.
- `tracing.py`: context-scoped request and task-turn counters.
- `replay.py`: exact and task-turn-compatible lookup.
- `injection.py`: protocol-aware L1-L4 intervention.
- `l3_dynamic.py` / `l4_optimizer.py`: dynamic continuation rewriting.
- `tool_defense.py`: deterministic inspection of action-bearing tool calls.
- `timeout_utils.py`: recursive clamping of tool timeout fields to 300 seconds.

`cursor_native.py` authenticates task routing credentials, exchanges the router-held
Cursor account key for a short-lived upstream token, and streams native RPC bytes
into per-context `native_traces/`. The CLI uses its HTTP/1 SSE fallback.
`cursor_wire.py`, `cursor_session.py`, and `cursor_replay.py` decode native model
steps, call the shared intervention/defense code, correlate executor results, and
replay recorded streams with client-message barriers. Native step boundaries and
classifier RPCs remain distinct from direct model API requests.

### `swebench_container/`

The container module resolves official SWE-bench task images, adds coding-agent CLI tooling, runs the agent in `/testbed`, and captures container output. It receives only the task, agent settings, router URL/key, artifact path, and timeout.

## Router modes

- `proxy-record`: call the upstream provider and persist the clean request/response path.
- `replay`: return a response from a recorded trace.
- `replay-inject`: replay the recorded response and intervene at the configured task turn.
- `inject`: call the upstream provider on each turn and intervene at the configured task turn.

The first three modes define the stable record/replay experiment. `inject` is an online convenience mode for fresh trajectories.

## Intervention levels

- **L1 — Response Substitution:** replace the provider response.
- **L2 — Response Append:** preserve the provider response and append a forged action.
- **L3 — LLM-Polished Injection:** rewrite the assistant text against the live task trajectory while retaining the selected action.
- **L4 — Distribution Alignment:** ask the configured multi-source generation service for a task-compatible continuation and tool selection; dataset tool arguments remain locked.

L3 and L4 are mutually exclusive. Every dynamic request, raw response, parse result, retry, and final injection decision is stored beside the target turn.

## Request lifecycle

1. Extract the API key and resolve its `ExperimentContext`.
2. Acquire the request gate.
3. Classify the request role and allocate `turn_index`; allocate `task_turn_index` only for main task traffic.
4. Persist `request.agent.json`.
5. Proxy or replay the clean response.
6. Apply the configured intervention when the resolved task turn matches.
7. Recursively clamp injected tool timeout fields.
8. Apply tool defense when enabled.
9. Persist before/after, replay, injection, defense, and dynamic-generation artifacts.
10. Mark the context terminal after the target injection/replay boundary.
11. Render JSON or protocol-correct SSE and release the gate.

If upstream processing fails, the allocated task turn is rolled back so a retry sees the same injection target.

## Replay correctness

`exact_request_hash` requires the recorded endpoint and stable request hash to match the live request. `task_turn_compatible` aligns main-agent task turns despite auxiliary title/reviewer drift, then rejects replay if the recorded response references a tool unavailable in the live request.

Replay metadata records whether the response matched exactly, matched by task turn, failed because of tool incompatibility, or could not find the requested source turn.

## Concurrency and terminal state

Turn state is scoped by run/context, never globally. A context lock prevents overlapping requests from advancing one context twice. A process-wide single-flight gate prevents concurrent upstream or L4 connections from outliving the experiment context that owns them.

After an injection or bounded replay completes, subsequent main-agent requests receive a terminal response. Reviewer traffic may still be recorded where the experiment requires observing the review path.

## Safety invariants

- Raw payloads require `payload_execution_allowed=true`.
- Prompts never encode experiment controls.
- The task container never mounts host secrets or the Docker socket.
- `review_outcome` and `execution_outcome` remain separate.
- API keys are redacted from public manifests and logs.
- Runtime output stays under ignored `artifacts/`.
