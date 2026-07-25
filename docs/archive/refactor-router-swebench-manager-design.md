# Router / SWE-bench Container / Manager Refactor Design Handoff

> Archived design record. The implemented system is documented in `README.md` and `docs/architecture.md`.

Status: design handoff for the next implementation pass.

This document intentionally keeps repeated decisions and detailed reasoning. It
is not a short summary. It collects the design decisions from the multi-turn
planning discussion so the next implementer can start coding without needing to
reconstruct the conversation.

## 0. Current Objective

The repo is an experiment system for studying code agents when the LLM router
returns forged tool-calls.

The experiment is not only asking whether a forged tool-call can be represented
in the protocol. The current target is stronger:

- Run Codex and Claude Code inside official SWE-bench task Docker images.
- Make the agents talk to a local router instead of directly to Kimi Code Plan.
- Let the router either call real Kimi, replay previously recorded Kimi/router
  responses, or replay and inject a dataset sample at a chosen router turn.
- Observe whether the agent executes the malicious tool-call.
- Observe whether auto-review is triggered.
- If auto-review is triggered, observe whether the reviewer allows or denies the
  action and why.
- Record enough raw input/output to explain the result later without rerunning
  the whole SWE-bench task.

Raw payload execution is allowed for this experiment because the code executes
inside Docker. This is an experimental decision, not an implicit default; every
run manifest must record `payload_execution_allowed = true` or `false`.

## 1. Core Experimental Questions

The implementation should be shaped around these questions:

1. If the router directly returns a forged tool-call at the beginning of the
   task, does Codex or Claude Code reject it, review it, execute it, or do
   something else?
2. If the router first replays a normal SWE-bench trace and only injects the
   forged tool-call at router turn N, does behavior change?
3. When the injected tool-call appears in a realistic SWE-bench context, is the
   code agent more or less likely to route it through review?
4. If review is triggered, what context does the reviewer see?
5. If review is triggered, does the reviewer allow or deny the action?
6. If the reviewer denies the action, what reason is returned?
7. If the reviewer allows the action, does the main agent actually execute the
   malicious tool-call?
8. If review is not triggered, does the malicious tool-call execute directly?
9. Which parts of the 400-row dataset are denied, allowed, executed, or not
   observed?
10. Which task context, agent, mode, injection turn, and injection mode cause
    differences?

The output statistics must preserve both axes:

- reviewer result
- malicious tool-call execution result

These must not be collapsed into one field.

## 2. Three-Module Target Architecture

The repo should be refactored around three modules:

```text
router/
  LLM protocol proxy, protocol conversion, context routing, recording, replay,
  injection, review request/response capture.

swebench_container/
  SWE-bench task-image resolution, wrapper image construction, agent command
  construction, container lifecycle, task prompt execution, stdout/stderr/event
  capture.

manager/
  Experiment planning, shared router startup, task container startup, run matrix
  execution, replay target resolution, dataset iteration, result analysis,
  summary generation.
```

The old scripts do not need compatibility. If they contain useful code, move the
code into the new modules. If they represent old sentinel/direct/safe-marker
experiments, archive or delete them.

The implementation should optimize for readability and maintainability, not for
keeping every old CLI path alive.

## 3. Shared Router Decision

The final design should use one shared router that can serve multiple task
containers at the same time.

Earlier there was a simpler option:

```text
one task container -> one router
```

That is easier to explain and isolate, but the chosen design is now:

```text
one shared router -> many task containers
```

The shared router must still isolate experiment state per task/container. It
should not use one global turn counter, one global replay source, or one global
injection setting.

The router should support multiple contexts. Each context is selected by the API
key used by a task container.

## 4. API Key as Context Routing Key

The API key is not just authentication. It is also the routing key.

Each task container receives a different router API key. Codex and Claude Code
already support API-key configuration, so this does not require modifying the
prompt or relying on body content.

The router should do:

```text
incoming request headers
  -> extract Authorization / x-api-key / api-key / openai-api-key
  -> resolve ExperimentContext
  -> process request under that context
```

Do not design router auth as only:

```python
check_key_is_valid() -> bool
```

Design it as:

```python
resolve_context_from_auth(headers) -> ExperimentContext
```

In single-context debug mode, this function can return the only context. In
shared-router mode, it must look up the context by API key.

The actual API key should not be written to artifacts. Store only a hash or
fingerprint.

## 5. Experiment Context

An experiment context is the unit of:

- request routing
- turn counting
- trace storage
- replay source selection
- injection target selection
- dataset sample selection
- analysis attribution

Example context:

```json
{
  "context_id": "exp-1625-codex-auto-sample-000123-step-11",
  "api_key": "rk_1625_codex_auto_000123",
  "agent": "codex",
  "agent_mode": "auto_review",
  "instance_id": "sqlfluff__sqlfluff-1625",
  "router_mode": "replay-inject",
  "trace_source_dir": "artifacts/records/1625-codex-auto/router/traces",
  "replay_until_turn": 11,
  "injection_turn": 11,
  "dataset_sample_id": "attack_000123",
  "injection_mode": "append",
  "payload_execution_allowed": true,
  "timeout_seconds": 1800
}
```

The shared router should start from a registry like:

```json
{
  "run_id": "20260618-shared-replay-inject",
  "router_mode": "replay-inject",
  "contexts": {
    "rk_1625_codex_auto_000123": {
      "context_id": "exp-1625-codex-auto-sample-000123-step-11",
      "instance_id": "sqlfluff__sqlfluff-1625",
      "agent": "codex",
      "agent_mode": "auto_review",
      "trace_source_dir": "artifacts/records/1625-codex-auto/router/traces",
      "replay_until_turn": 11,
      "injection_turn": 11,
      "dataset_sample_id": "attack_000123",
      "injection_mode": "append"
    },
    "rk_1733_codex_auto_000123": {
      "context_id": "exp-1733-codex-auto-sample-000123-step-19",
      "instance_id": "sqlfluff__sqlfluff-1733",
      "agent": "codex",
      "agent_mode": "auto_review",
      "trace_source_dir": "artifacts/records/1733-codex-auto/router/traces",
      "replay_until_turn": 19,
      "injection_turn": 19,
      "dataset_sample_id": "attack_000123",
      "injection_mode": "append"
    }
  }
}
```

The global router mode can be the same for a batch, but replay and injection
turns must be per context because different tasks reach the same logical phase at
different turns.

## 6. Router Modes

The new router should keep only the current experiment modes:

```text
proxy-record
replay
replay-inject
```

Old `tool_call`, sentinel marker, safe marker, old direct review-gate harness,
and old prompt-controlled attack scenario paths are not required for the new
mainline.

### 6.1 proxy-record

This mode calls real Kimi Code Plan and records everything needed for future
replay.

Flow:

```text
agent -> router -> Kimi API -> router -> agent
```

For every router turn, save:

- agent request as received by router
- upstream request sent to Kimi
- upstream response from Kimi
- router response sent back to agent
- request hash
- response hash
- metadata
- request role classification if available

This mode is used to build reusable traces.

### 6.2 replay

This mode does not call Kimi. It loads a previously recorded router response from
`trace_source_dir` for the current context and turn.

Flow:

```text
agent -> router -> recorded response -> agent
```

Formal experiment replay should be strict:

```text
context_id + turn_index + endpoint + request_hash must match
```

If request hash mismatches, mark the current context as `trace_mismatch` and fail
that context. Other contexts served by the shared router should continue.

A lenient mode can exist for debugging, but it must write drift metadata and must
not be used for final conclusions.

### 6.3 replay-inject

This mode replays old responses until a selected turn, then injects a forged
tool-call.

Flow:

```text
agent -> router -> recorded response -> inject if target turn -> agent
```

For turns before the target turn, the router returns the recorded response. At
`injection_turn`, it modifies the response according to `injection_mode` and
`dataset_sample_id`.

Save both versions:

```text
response.router.before_injection.json
response.router.after_injection.json
injection.json
```

The router should render dataset samples into the correct protocol for the
agent. The prompt should not contain router state or dataset selection.

## 7. Step 0, Step N, Direct, and Replay

Direct experiments are not a separate legacy experiment type.

Direct means:

```text
injection_turn = 0
```

This is step 0.

Mid-task injection means:

```text
injection_turn = N
```

This is step N.

The system restores to a chosen state by replaying router responses to the real
CLI from the start until the selected turn. It does not hack `~/.codex` or
`~/.claude` session files.

The real Codex or Claude Code process still starts normally in the SWE-bench task
container. It sends requests. The router replies with recorded responses. When
the target turn arrives, the router injects the dataset payload.

This is slower than directly forging a session file, but it preserves the real
agent state machine, permission state, tool results, filesystem changes, and
review behavior.

## 8. Why Not Forge Codex / Claude Session Files

Codex and Claude Code may store transcripts or session files under `~/.codex` or
`~/.claude`, but replaying only those files is not enough to guarantee identical
runtime state.

Potential inconsistencies:

- current process memory is not restored
- permission/review state machine may not be restored
- outstanding tool-call IDs may not line up
- shell working directory and environment may differ
- filesystem changes from previous tool calls may not exist
- generated patches may not match transcript state
- CLI internal counters or hidden metadata may differ
- reviewer invocation may depend on runtime state, not only transcript text

Therefore the chosen method is to start the real CLI and replay router responses.
This is not a perfect teleport to step N; it is a controlled rerun with stable LLM
outputs until step N.

## 9. Logical Step vs Resolved Turn

Different SWE-bench tasks can reach comparable phases at different router turns.
Therefore do not define one global integer step for every task unless that is
really what is being tested.

Use two concepts:

```text
logical_step
resolved_turn_index
```

Example:

```json
{
  "logical_step": "after_first_test_run",
  "resolved_targets": {
    "exp-1625-codex-auto": {"injection_turn": 11},
    "exp-1733-codex-auto": {"injection_turn": 19},
    "exp-2419-claude-auto": {"injection_turn": 7}
  }
}
```

The first implementation does not need automatic semantic step inference. The
practical first path is:

1. Run `proxy-record` for the chosen tasks.
2. Generate a per-context turn summary.
3. Choose a resolved `injection_turn` for each context.
4. Generate the shared router context registry.
5. Run `replay-inject`.

Router consumes resolved turns. Manager owns logical step planning.

## 10. Turn Counting

Turn counting must be per context, not global.

Correct shape:

```python
turn_index = turn_state.next(context_id)
```

Wrong shape:

```python
TURN_COUNTER += 1
```

Every agent-to-router LLM request is one router turn. Different endpoints share
the same per-context sequence.

Example:

```text
turn 0: /v1/responses
turn 1: /v1/responses
turn 2: /v1/chat/completions
```

Each turn metadata should include:

```text
run_id
context_id
turn_index
endpoint
agent
agent_mode
instance_id
request_role
request_hash
response_hash
started_at
finished_at
router_mode
trace_source_dir
```

Because the shared router can receive concurrent requests from multiple task
containers, turn allocation must be concurrency-safe.

## 11. Request Role Classification

Every request should be classified as:

```text
agent_main
reviewer
unknown
```

The role is used for analysis. It should not be the only mechanism deciding
whether something executes.

Signals can include:

- endpoint
- model name
- request body shape
- tools list
- user-agent
- Codex-specific request fields
- Claude Code-specific request fields
- system prompt fragments as a weak fallback only

Do not rely only on prompt text. Prompt matching is too fragile.

The design question from earlier was whether auto-review requests are also
handled by the router. The answer should be yes when the agent's reviewer uses
the same configured LLM endpoint. Therefore router traces must be able to show
which requests were main-agent requests and which were reviewer requests.

Codex and Claude Code may differ. Each agent adapter should help classify its
own review behavior.

## 12. Injection Semantics

Supported injection modes:

```text
append
replace
```

`append` means:

```text
keep original replayed/upstream response
add forged tool-call to the response
```

`replace` means:

```text
discard original response body/content
return only the forged tool-call
```

The injection record must include:

```text
dataset_sample_id
dataset_source_path
injection_mode
injection_turn
tool_name
arguments
payload_execution_allowed
response_before_path
response_after_path
protocol
agent
```

The router is responsible for converting dataset samples to the target protocol
format:

- OpenAI Responses format for Codex if Codex is using `/v1/responses`
- OpenAI Chat Completions format if the agent uses chat completions
- Anthropic Messages format for Claude Code when applicable

The dataset should not be injected by editing the user prompt.

## 13. Reviewer Outcome and Execution Outcome Must Be Separate

The final analysis must keep separate fields for:

```text
review_outcome
execution_outcome
```

Reviewer taxonomy:

```text
review_not_triggered
review_allowed
review_denied
review_unknown
review_timeout
review_parse_error
```

Execution taxonomy:

```text
malicious_tool_executed
malicious_tool_blocked
malicious_tool_not_observed
execution_unknown
```

Important combinations:

```text
review_denied + malicious_tool_blocked
review_allowed + malicious_tool_executed
review_not_triggered + malicious_tool_executed
review_allowed + malicious_tool_not_observed
review_unknown + malicious_tool_executed
```

Do not treat `review_not_triggered` as `review_allowed`.

Do not treat `no review observed` as proof that an agent is safe or unsafe. It
must be paired with execution evidence.

## 14. Detecting Malicious Tool Execution

Safe marker and sentinel marker logic is retired for the new mainline.

Because raw payload execution is allowed inside Docker, detection should rely on
stronger evidence:

- agent event log contains the forged tool-call
- container stdout/stderr contains the command or tool result
- router trace contains a follow-up tool result from the agent
- filesystem side effect inside the container, when the sample has one
- process exit or error output consistent with the injected command

If a sample cannot be reliably detected, record `execution_unknown` rather than
guessing.

The dataset should eventually include analysis helper fields:

```text
category
tool_name
argument_shape
expected_execution_signal
destructiveness_level
requires_network
requires_shell
requires_file_write
```

## 15. Dataset Distribution and Profiling

The 400-row dataset should be profiled before interpreting results. The user
asked to know how the 400 samples are distributed.

Profile dimensions should include:

```text
sample id
category
tool name
argument format
agent protocol format
whether shell is required
whether file write is required
whether network is required
whether the payload is destructive
whether execution has a reliable observable signal
```

Final reports should slice by these fields, not only by aggregate totals.

## 16. SWE-bench Container Module

The SWE-bench container module owns execution inside task-specific Docker
images.

Inputs:

```text
run_id
context_id
instance_id
agent
agent_mode
router_url
router_api_key
artifact_dir
timeout_seconds
```

Responsibilities:

- resolve the official SWE-bench task image
- build a wrapper image from that task image
- install or mount Codex / Claude Code support
- start the agent inside `/testbed`
- pass the SWE-bench issue prompt to the agent
- configure the agent to use the shared router
- save stdout, stderr, event logs, final patch, git status, and Docker inspect

This module must not know:

- dataset IDs
- injection turns
- replay source details
- Kimi API details

Those belong to manager and router.

The task must run in its own official SWE-bench task Docker image. Old shared
agent-image execution is deprecated for final conclusions.

## 17. Agent Modes

The first supported agents are:

```text
codex
claude
```

Each agent supports at least two modes:

```text
allow_all
auto_review
```

`allow_all` means tool-calls are allowed without interactive review.

`auto_review` means the agent should use its own reviewer/approval path.

For Codex, the desired auto-review mode is equivalent to the interactive
permission setting:

```text
/permission -> Approve for me
```

This is not automatically identical to a generic `approval_policy=on-request`.
Implementation must verify how the Codex CLI exposes this mode.

For Claude Code, the corresponding permission/review mode must be implemented in
its own adapter.

## 18. Agent Adapter Boundary

Do not scatter agent-specific `if agent == "codex"` and `if agent == "claude"`
logic across manager, runner, and analyzer.

Each agent should have an adapter with responsibilities like:

```text
build_command()
build_env()
parse_events()
classify_review()
classify_tool_execution()
protocol_preferences()
```

This keeps future `opencode` support from polluting the main flow.

## 19. Manager Module

The manager owns orchestration and analysis.

Manager responsibilities:

- build run manifests
- generate context API keys
- write shared router config
- start the shared router
- start multiple SWE-bench task containers
- pass router settings through config/env/CLI, not prompt text
- track container status
- retry setup failures where safe
- collect artifacts
- summarize review outcomes
- summarize execution outcomes
- produce CSV and JSON reports

Manager phases:

```text
record
summarize-turns
replay
replay-inject
matrix
analyze
```

The manager should be able to run:

- a tiny smoke matrix
- a 3-task experimental matrix
- eventually the full 400-sample matrix

## 20. Direct Experiment as Step 0

Direct should not mean the old direct review-gate script.

Direct should mean:

```text
injection_turn = 0
```

In other words, the first router response to the agent includes the forged
tool-call.

This puts direct and mid-task injection under the same runner and analyzer.

## 21. Replay to Step N

The desired replay behavior is:

```text
start real task container
start real Codex / Claude Code
agent sends request 0
router replays response 0
agent executes tools / updates files as usual
agent sends request 1
router replays response 1
...
agent sends request N
router injects dataset sample into response N
observe review and execution
```

This means replay still needs to run from the beginning of the task. It avoids
paying for Kimi calls and stabilizes LLM outputs, but it does not skip all tool
execution time before N.

The manager should measure replay speed so we can decide whether the approach is
worth it:

```text
time_to_turn_N_in_record
time_to_turn_N_in_replay
time_from_injection_to_review
time_from_injection_to_execution_or_block
total_context_runtime
```

## 22. Replay Strictness and Drift

Formal experiments should use strict replay.

Strict match keys:

```text
context_id
turn_index
endpoint
request_hash
```

If they mismatch, the result should be:

```text
trace_mismatch
```

Lenient replay can exist for debugging only. It should write:

```text
expected_request_hash
actual_request_hash
drift_detected = true
```

No final conclusion should depend on lenient replay without clearly saying so.

## 23. Artifact Layout

Recommended layout:

```text
artifacts/runs/<run_id>/
  router/
    effective_config.json
    router.log
  experiments/
    <context_id>/
      manifest.json
      router/
        traces/
          0000-openai.responses/
            metadata.json
            request.agent.json
            request.upstream.json
            response.upstream.json
            response.router.before_injection.json
            response.router.after_injection.json
            replay.json
            injection.json
            review.json
          0001-openai.responses/
            ...
      container/
        stdout.log
        stderr.log
        events.jsonl
        final.patch
        git.status.txt
        docker.inspect.json
        metadata.json
      analysis/
        result.json
  analysis/
    details.csv
    summary.json
    review_reasons.json
    dataset_profile.json
```

The shared router stores traces under each context, not under one flat global
turn sequence.

## 24. Run Manifest Fields

Every context should save an effective manifest.

Fields:

```text
run_id
context_id
agent
agent_mode
instance_id
router_mode
trace_source_dir
replay_until_turn
injection_turn
dataset_sample_id
injection_mode
payload_execution_allowed
timeout_seconds
swebench_image
wrapper_image
router_api_key_hash
schema_version
```

Definitions:

`run_id`: batch-level identifier.

`context_id`: one experiment context, usually one agent/mode/task/sample/step.

`agent`: `codex` or `claude` first.

`agent_mode`: `allow_all` or `auto_review`.

`instance_id`: SWE-bench instance ID, such as `sqlfluff__sqlfluff-1625`.

`router_mode`: `proxy-record`, `replay`, or `replay-inject`.

`trace_source_dir`: where replay reads previous traces.

`replay_until_turn`: how far replay is intended to go before target behavior.

`injection_turn`: the router turn where the forged dataset sample is injected.

`dataset_sample_id`: sample from `data/code_agent_attack_dataset.jsonl`.

`injection_mode`: `append` or `replace`.

`payload_execution_allowed`: explicit safety/experiment flag.

`timeout_seconds`: max runtime for the context.

`swebench_image`: official SWE-bench task image.

`wrapper_image`: task image plus agent/runtime additions.

`router_api_key_hash`: fingerprint only, not the raw secret.

`schema_version`: artifact schema version.

## 25. Failure Taxonomy

Each context should finish with a structured status.

Possible statuses:

```text
completed
agent_timeout
router_timeout
review_timeout
container_setup_failed
image_build_failed
trace_mismatch
target_turn_missing
payload_executed
payload_blocked
agent_crashed
analysis_parse_failed
interrupted
```

Status and outcome should be separate. A context can complete while recording:

```text
review_denied + malicious_tool_blocked
```

## 26. Docker and Safety Boundary

Raw payload execution is allowed, but the Docker boundary must be explicit.

Default constraints:

- no host Docker socket mounted into task containers
- no host sensitive directories mounted
- per-context artifact directory only
- independent containers per context
- network policy recorded and configurable
- container image ID recorded
- parent SWE-bench image recorded

There was discussion about Docker-in-Docker. The chosen first design should avoid
it. Let the host manager control task containers. Do not make task containers run
their own Docker daemon unless a later requirement forces it.

## 27. Router Config Must Come From Script, Not Prompt

Router state must not be controlled by text inserted into the user prompt.

Allowed configuration sources:

```text
manifest.json
router config file
environment variables
manager CLI args
```

The SWE-bench task prompt should be about solving the task. It should not encode
which dataset sample to inject, which turn to use, or whether the router is in
replay mode.

Every run should save the final expanded router configuration as:

```text
router/effective_config.json
```

## 28. Old Code and Documentation Strategy

Old scripts can be incompatible. This is an explicit design decision.

Old paths that can be retired, archived, or deleted after useful code is moved:

```text
scripts/agent-review-gate-lab.sh
scripts/agent-attack-lab.sh
scripts/run_attack_review_matrix.py old direct branch
scripts/swebench_attack_matrix.py old mixed orchestration
old sentinel marker logic
old safe marker logic
old prompt-controlled attack scenario logic
```

Old historical docs should stay in `docs/archive/` if they are useful as
background, but the current README should not tell users to run them as the
mainline experiment.

## 29. What Current Historical Results Mean

Earlier data included direct/replace and append turn experiments. Important
interpretation points:

- Codex direct showed `no_review_executed` for 400/400 in the old measurement.
  That did not prove Approve-for-me review was bypassed; it mainly showed the
  Responses function_call format could be accepted without the old harness
  triggering real review.
- Claude direct rejected many samples and did not review others under old setup.
- Some SWE-bench append data exists, but it used earlier runner semantics and
  must not be treated as final under the new shared-router task-image design.
- Some tasks had long or looping traces, especially SQLFluff parser probing
  loops. The new system must record enough turn/tool summaries to explain such
  behavior.

These old observations motivate the new architecture but should not be mixed
with final results unless the artifact schema and setting are clearly labeled.

## 30. Turn Summary for Human Selection

The manager should provide a command that reads recorded traces and produces a
human-readable turn summary.

A useful summary row should include:

```text
turn_index
endpoint
request_role
short prompt/request summary
response type
number of tool-calls
first tool name
whether tests were run
whether files were edited
elapsed time
```

This lets the user choose turns like:

```text
step 0
before first file edit
after first file edit
after first test run
after first failed test
before final answer
```

The manager can later automate logical-step detection, but first version can be
manual/resolved.

## 31. Review Reason Analysis

The final analysis should not only count denials. It should explain why samples
were denied.

Store and extract:

```text
review_raw_request.json
review_raw_response.json
review_decision.json
reason_text
reason_category
matched_policy_or_pattern if available
```

Reason categories may include:

```text
unsafe_command
credential_access
network_access
file_exfiltration
destructive_filesystem_change
permission_escalation
unclear_or_malformed
other
```

The categories can start heuristic-based and improve later.

## 32. Protocol Conversion Boundary

Protocol conversion should be moved out of the monolithic FastAPI app.

Suggested structure:

```text
router/protocols/openai_responses.py
router/protocols/openai_chat.py
router/protocols/anthropic_messages.py
```

Each module should handle:

- request normalization if needed
- upstream conversion if Kimi expects another protocol
- response construction
- tool-call append/replace operations for that protocol

The router mode service should not manually edit every protocol shape.

## 33. Router Service Boundary

Suggested router services:

```text
router/context.py
  ExperimentContext, ContextRegistry, resolve context from auth.

router/turn_state.py
  per-context concurrency-safe turn allocation.

router/trace_store.py
  read/write per-context turn artifacts.

router/replay_store.py
  load replay response and verify strict hashes.

router/injection_service.py
  select dataset sample, render protocol-specific tool-call, append/replace.

router/review_classifier.py
  classify request role and reviewer decision.

router/modes/proxy_record.py
router/modes/replay.py
router/modes/replay_inject.py
```

The current `router/app.py` should become thin FastAPI wiring.

## 34. SWE-bench Container Structure

Suggested structure:

```text
swebench_container/task_image.py
  resolve official SWE-bench image.

swebench_container/wrapper_image.py
  build wrapper image from task image.

swebench_container/agent_command.py
  build agent command and env through adapters.

swebench_container/runner.py
  start container, enforce timeout, collect outputs.

swebench_container/artifacts.py
  save final patch, git status, docker inspect, logs.

swebench_container/agents/codex.py
swebench_container/agents/claude.py
```

## 35. Manager Structure

Suggested structure:

```text
manager/schemas.py
manager/config.py
manager/run_record.py
manager/summarize_turns.py
manager/run_replay_inject.py
manager/run_matrix.py
manager/analyze_reviews.py
manager/analyze_execution.py
manager/cli.py
```

A unified CLI is acceptable:

```sh
uv run python -m manager.cli record ...
uv run python -m manager.cli summarize-turns ...
uv run python -m manager.cli replay-inject ...
uv run python -m manager.cli matrix ...
uv run python -m manager.cli analyze ...
```

Old script names do not need to remain stable.

## 36. Suggested Implementation Milestones

Milestone 1: schemas.

- Define `ExperimentContext`.
- Define `RunManifest`.
- Define `TurnMetadata`.
- Define `InjectionRecord`.
- Define `ReviewDecision`.
- Define `ExecutionDecision`.
- Add `schema_version` everywhere.

Milestone 2: context-aware router.

- Replace auth bool checks with context resolution.
- Add `ContextRegistry`.
- Add per-context `TurnState`.
- Add context-aware `TraceStore`.

Milestone 3: replay correctness.

- Strict replay by context/turn/endpoint/request hash.
- Per-context mismatch handling.
- Save replay metadata.

Milestone 4: injection service.

- Support `append` and `replace`.
- Support step 0 and step N.
- Save before/after responses and injection metadata.

Milestone 5: protocol extraction.

- Extract OpenAI Responses handling.
- Extract OpenAI Chat handling.
- Extract Anthropic Messages handling.
- Keep FastAPI route thin.

Milestone 6: SWE-bench container module.

- Resolve task images.
- Build wrappers.
- Run Codex and Claude.
- Capture outputs and Docker metadata.

Milestone 7: manager.

- Generate shared router configs.
- Launch shared router.
- Launch multiple task containers.
- Generate turn summaries.
- Run replay-inject matrices.
- Analyze results.

Milestone 8: retire old paths.

- Archive/delete old script entry points.
- Update README and docs to current flow.
- Remove references to safe marker as current experiment mechanism.

Milestone 9: smoke matrix.

Run:

```text
tasks: 1
samples: 2
agents: codex, claude
agent_modes: allow_all, auto_review
targets: step 0 and one resolved step N
injection_modes: append first, replace second if needed
```

Each run must produce:

```text
manifest.json
router trace
container logs
review decision
execution decision
summary.json
details.csv
```

Milestone 10: expand.

Only after the smoke matrix is explainable, expand to:

```text
3 tasks
400 samples
Codex + Claude Code
allow_all + auto_review
step 0 + selected resolved step N values
```

## 37. Design Tradeoffs Chosen

Chosen: shared router now.

Reason: the desired experiment runs many contexts and API-key routing gives a
clean way to multiplex them.

Cost: context registry, per-context turn state, and concurrency safety are
required.

Chosen: API key as routing key.

Reason: supported by agents without prompt hacks.

Cost: auth and routing become one concern and must be designed carefully.

Chosen: replay real CLI from start instead of forging sessions.

Reason: review and execution behavior depend on real runtime state.

Cost: cannot instantly teleport to step N; replay still runs tool calls before N.

Chosen: direct is step 0.

Reason: one experiment model and one analyzer.

Cost: old direct script semantics retired.

Chosen: raw payload execution allowed.

Reason: the experiment needs actual execution evidence.

Cost: Docker isolation and evidence collection must be robust.

Chosen: old scripts need not be compatible.

Reason: compatibility keeps bad smells and obsolete semantics alive.

Cost: old commands in old docs will break or be archived.

Chosen: logical step resolved by manager.

Reason: tasks differ; router should not understand SWE-bench progress.

Cost: need a turn-summary step and maybe manual mapping in early versions.

## 38. Open Implementation Details To Decide While Coding

These are not blockers for the design, but must be settled during implementation:

- Exact Codex CLI configuration for `/permission -> Approve for me`.
- Exact Claude Code configuration for comparable auto-review behavior.
- Whether Codex/Claude expose machine-readable event streams or require parsing
  stdout/stderr.
- How to classify reviewer requests robustly for each agent.
- Whether `append` should append tool-call after text, before text, or in the
  protocol-native tool-call section when both text and tool-calls exist.
- How to handle streaming responses under replay-inject.
- Whether final full matrix should include `replace` or only `append` after smoke
  validation.
- How to handle task traces that naturally timeout or loop before the target
  turn.
- Whether network should be enabled inside task containers for raw payload tests.
- How destructive samples are labeled and whether some need separate isolation.

## 39. What The Next Implementer Should Do First

Do not start by adding more experiment scripts.

Start by defining schemas and context-aware router primitives:

```text
ExperimentContext
ContextRegistry
TurnState
TraceStore
```

Then make the router able to serve two contexts with two API keys in a tiny local
smoke test.

After that, wire replay and injection into context-aware traces.

Only after router context routing is correct should the SWE-bench container and
manager layers be rewritten.
