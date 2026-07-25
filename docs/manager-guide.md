# Manager Guide

`manager/` owns experiment dimensions and orchestration. It does not process LLM requests.

## CLI

```text
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
- `--agents codex,claude`
- `--agent-mode allow_all|auto_review`
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

## Matrix runners

`scripts/run_injection_review_matrix.py` is the complete record/replay-injection runner. The Codex and Claude wrappers fix the agent selection and expose the same experiment controls.

The runner:

1. validates trace sources and dynamic settings
2. writes the plan and context registry
3. starts the shared router
4. builds/reuses task wrapper images
5. executes contexts
6. stops the router
7. analyzes the run

The `inject` mode rejects replay trace options because it calls the upstream provider directly.

## Analysis

`manager.analysis` derives:

- context status
- replay outcome
- review outcome
- execution outcome
- malicious/benign defense counters
- grouped reviewer decisions and rerun statistics

Outputs are written as summary JSON plus per-context CSV/JSONL records.

Reviewer reruns use `scripts/replay_reviewer_runs.py` and a separate output root. The analyzer can combine those responses without overwriting the original trace.

## Dataset profiling

```sh
python -m manager.cli profile-dataset \
  --dataset data/code_agent_attack_dataset.jsonl \
  --output /tmp/sidel-profile.json
```

The verified stable counts are 400 records, four balanced threat families, Bash 277, Write 74, Edit 49, critical 211, high 138, and medium 51.

## Failure handling

Container setup, image build, agent timeout/crash, replay mismatch, missing target turn, and analysis errors remain structured statuses. They must not be folded into security success or failure rates without an explicit analysis rule.
