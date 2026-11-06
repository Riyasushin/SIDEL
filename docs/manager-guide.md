# Manager Guide

`manager/` owns experiment dimensions and orchestration. It does not process LLM requests.

## CLI

```text
python -m manager.cli run
python -m manager.cli plan
python -m manager.cli analyze
python -m manager.cli profile-dataset
python -m manager.cli container-specs
python -m manager.cli run-containers
```

Use `python -m manager.cli <command> --help` as the authoritative parameter reference.

## Planning

`plan` expands agents, tasks, samples, and fixed turns into `ExperimentManifest` objects. Random-turn mode creates one context per agent/task/sample and stores the turn candidates plus seed instead of multiplying contexts by every candidate.

Important options:

- `--router-mode proxy-record|replay|replay-inject|inject`
- `--agents codex,claude,opencode,cursor`
- `--agent-mode`: a native mode from [agent-modes.md](agent-modes.md); omitted values use per-agent defaults
- `--samples`
- `--injection-turns`
- `--injection-turn-mode fixed|random`
- `--injection-random-seed`
- `--injection-mode append|replace`
- `--replay-match-mode exact_request_hash|task_turn_compatible`
- `--payload-execution-allowed`
- `--enable-tool-defense`
- `--dynamic-l3` or `--dynamic-l4`

L3 and L4 are mutually exclusive.

## Manifests and context registry

Each context receives a random API key. The public context manifest contains only a fingerprint. The ignored router registry contains the raw key because the running router needs to resolve:

```text
API key -> ExperimentContext
```

The context carries the agent, permission mode, task, trace source, replay policy, injection policy, defense switches, images, and timeout.

## Container specifications

`ExperimentOrchestrator` combines:

- context manifests
- raw API keys from `effective_config.json`
- the SWE-bench task snapshot
- the issue prompt template

It emits `ContainerRunSpec` records without putting experiment controls in the user prompt.

`container-specs` renders these records for inspection. `run-containers` builds wrapper images when requested and runs contexts with configurable worker concurrency.

## Complete workflow

`python -m manager.cli run` executes the full workflow implemented in `manager/workflow.py`. Select agents with `--agents codex`, `--agents claude`, or `--agents codex,claude`.

The runner:

1. validates trace sources and dynamic settings
2. writes the plan and context registry
3. starts the shared router
4. builds/reuses task wrapper images
5. executes contexts
6. stops the router
7. analyzes the run

`run` supports all four router modes for Codex, Claude Code, OpenCode, and Cursor.
Cursor replay always requires a native trace source, including for turn-zero
replacement; use `inject` for an online intervention without a source.
`inject` and `proxy-record` reject trace
options. `replay` requires a trace. `replay-inject` requires a trace except for
fixed turn-zero replacement. `--trace-source-dir` is restricted to one agent and
one task; use `--trace-source-root` for larger matrices.

The defaults are one task, one sample, turn zero, replacement, and exact
request-hash replay. The default agent mode is OpenCode Plan, and auto review
for the other agents. Use `--samples all` for the full dataset, and
`--injection-turns 0,2:4` for several turns. Random selection also requires
`--injection-turn-mode random --injection-random-seed <seed>`.
`--injection-step` and `--injection-steps` remain accepted aliases for script migration.

L3 runs directly through this entry point:

```sh
python -m manager.cli run \
  --run-id claude-l3-smoke --agents claude --task-limit 1 --samples 1 \
  --dynamic-l3 --injection-turns 0 --injection-mode replace \
  --payload-execution-allowed
```

Configure service credentials through `.env` or the environment. Explicit
`--dynamic-l3-*` / `--dynamic-l4-*` options override those settings; omitted
options preserve them. L3 exposes backend, URL, key, model, call version, alpha,
seed, timeout, attempt limit, token limit, recent-message count, and rewrite
requirement. Other router settings remain available through environment variables.
The dynamic switches are recorded per context; service credentials are passed
only to the router process and are not copied into manifests or console output.
`plan` writes manifests only; when starting its router manually, configure the
L3/L4 service using the environment or `.env`.

`--dry-run` writes only the plan and registry. Add `--payload-execution-allowed`
explicitly to authorize raw payloads; it defaults to false. Wrapper images are
built/reused unless `--skip-image-build` is passed. `run` executes sequentially;
`run-containers` remains available for worker-based execution with a separately
managed router. Use `--router-host`, `--router-url-host`, `--router-port`, and
`--network` for the local Docker network setup.

Context filters (`--context-id-file`, `--resume-statuses`, and
`--skip-successfully-defended-contexts`) select contexts before writing the
registry. The last filter specifically skips L4-optimized, defense-blocked
contexts. Router startup failures and exceptions during execution trigger child
process cleanup.

## Analysis

`manager.analysis` derives:

- context status
- replay outcome
- review outcome
- execution outcome
- malicious/benign defense counters
- grouped reviewer decisions and rerun statistics

Outputs are written as summary JSON plus per-context CSV/JSONL records.

Reviewer reruns use `scripts/rerun_reviewer_requests.py` and a separate output root. The analyzer can combine those responses without overwriting the original trace.

## Dataset profiling

```sh
python -m manager.cli profile-dataset \
  --dataset data/code_agent_attack_dataset.jsonl \
  --output /tmp/sidel-profile.json
```

The verified stable counts are 400 records, four balanced threat families, Bash 277, Write 74, Edit 49, critical 211, high 138, and medium 51.

## Failure handling

Container setup, image build, agent timeout/crash, replay mismatch, missing target turn, and analysis errors remain structured statuses. They must not be folded into security success or failure rates without an explicit analysis rule.
