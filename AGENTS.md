# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

SIDEL is a security-research framework for measuring router-side response intervention in coding-agent workflows. It uses real SWE-bench task images and separates upstream output, router-delivered output, review evidence, and repository-level execution evidence.

## Architecture

```text
manager/             experiment planning, manifests, orchestration, analysis
router/              shared OpenAI/Anthropic router, replay, L1-L4, defense
swebench_container/  isolated Codex/Claude/OpenCode/Cursor task execution
```

The API key is the context-routing key. The manager writes an ignored `effective_config.json`; each task container receives a unique key resolving to one `ExperimentContext`.

## Commands

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python -m unittest discover -s tests -v
python -m compileall manager router swebench_container scripts tests
python -m manager.cli --help
python -m manager.cli run --help
python -m manager.cli profile-dataset --output /tmp/sidel-profile.json
```

`python -m manager.cli run` owns the complete plan/router/container/analysis
workflow, including `--dynamic-l3` / `--dynamic-l4` service options. Omitted
service options preserve environment / `.env` values. `run --dry-run` writes
only manifests and the registry. Raw payloads require the explicit
`--payload-execution-allowed` flag. Standalone utilities are located in `scripts/`.

Start a planned router:

```sh
ROUTER_CONTEXT_REGISTRY=artifacts/runs/<run_id>/router/effective_config.json \
ROUTER_ARTIFACT_ROOT=artifacts/runs \
python -m uvicorn router.app:app --host 127.0.0.1 --port 18080
```

Native mode mappings, protocol semantics and validation limits: `docs/agent-modes.md`.
Cursor uses a pinned native RPC adapter for record/replay/L1–L4/defense. Its task
turns follow `StepStarted`; classifier RPCs and executor results are separate.
`CURSOR_API_KEY` and `CURSOR_BASE_URL` are router-side settings.
Claude `bypassPermissions` runs as non-root `sidel`.

## Router behavior

- Modes: `proxy-record`, `replay`, `replay-inject`, `inject`.
- Replay: `exact_request_hash` or `task_turn_compatible`.
- Intervention: replace/append plus optional dynamic L3 or L4.
- Defense: optional deterministic tool-call scan before delivery.
- Turn counters, locks, terminal state, and artifacts are context-scoped.
- L3 and L4 are mutually exclusive.

## Invariants

- `task_turn_index=0` is the first real task request; reviewer/title/bootstrap requests do not advance it.
- Failed upstream attempts roll back the task turn.
- Exact replay mismatches fail closed.
- Task-turn-compatible replay must preserve tool compatibility.
- Prompts never carry dataset IDs, router modes, or injection turns.
- `review_outcome` and `execution_outcome` are always recorded separately.
- A tool error after invocation still counts as execution evidence.
- Raw payload execution requires `payload_execution_allowed=true`.
- Tool timeout fields are capped at 300 seconds before persistence/delivery.
- Never log, commit, or place real API keys in `.env.example`.

## Artifacts

Runtime output belongs under `artifacts/runs/<run_id>/` and is ignored. A clean checkout tracks only `artifacts/.gitkeep`. Never force-add a run directory without explicit curation and secret review.

## Documentation

The root README is the public source of truth. Current technical guides are located in `docs/`; `docs/archive/` is historical and must not be used as the active runbook.

Comments and internal docs may use Chinese, while public interfaces and identifiers remain English.
