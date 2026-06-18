# SWE-bench Baseline Workflow

This repo can run Kimi Code Plan backed SWE-bench Lite dev baselines through Docker-only code-agent containers.

## Boundaries

- Commit: runner scripts, router code, prompt templates, the fixed 23-task manifest, and docs.
- Do not commit: `.env`, API keys, cloned task worktrees, router traces, stdout/stderr logs, patches, raw SWE-bench reports, or generated predictions.
- Runtime data lives under `artifacts/swebench-baseline/`, which is intentionally gitignored.
- `KIMI_API_KEY` is read from environment or `.env` and passed only to the router container. Claude Code, Codex, and opencode get `local-router-key` plus the Docker router URL; agent containers do not mount `.env` and never receive the real Kimi key.

## Data Layout

```text
artifacts/swebench-baseline/
├── summary-<date>.json
└── runs/<run_id>/
    ├── run.json
    ├── tasks.jsonl
    ├── predictions.jsonl
    ├── summary.json
    ├── events.jsonl
    ├── router-traces/router-kimi.jsonl
    ├── router-traces/router-full.jsonl
    ├── router-traces/conversations/<agent>/<instance_id>/<turn>-<endpoint>/  # includes metadata.json with request_role
    │   ├── request.agent.json
    │   ├── response.router.before.json
    │   ├── response.router.after.json
    │   └── injection.json
    ├── evaluation/
    │   ├── report.json
    │   ├── summary.json
    │   ├── per_instance.jsonl
    │   └── per_instance.csv
    └── agents/<agent>/<instance_id>/
        ├── metadata.json
        ├── prompt.md
        ├── stdout.jsonl
        ├── stderr.log
        ├── final.patch
        ├── git.diff
        ├── git.status
        └── exit.json
```

The important distinction is:

- `metadata.status`: whether the agent process finished, failed, timed out, or was interrupted.
- `swebench_result`: whether the official SWE-bench harness marked the patch as `resolved`, `unresolved`, `empty_patch`, or `error`.

An agent can be `completed` and still be `unresolved`.

## Attack Injection and Transcript Capture

For attack experiments, keep `--execution-mode swebench-task-image`; the shared-agent-image mode is not valid for final SWE-bench conclusions. The runner starts a task-scoped router for each agent/task case and passes `ROUTER_TRACE_RUN_ID`, `ROUTER_TRACE_AGENT`, and `ROUTER_TRACE_INSTANCE_ID` through the router environment. The task prompt remains only the SWE-bench problem text and does not carry router state.

Use `ATTACK_INJECTION_MODE=off` to collect clean SWE-bench transcripts. Use `ATTACK_INJECTION_MODE=append` to call the upstream model normally and then append one dataset tool call to the selected turn. Use `ROUTER_MODE=tool_call` / `ATTACK_INJECTION_MODE=replace` only for the smaller permission-gate lab.

See `docs/agent-attack-experiment-plan.md` for the exact settings, commands, and interpretation rules.

## Execution Modes

The runner supports two patch-generation modes:

- `shared-agent-image` (deprecated legacy): clone the repo on the host, mount it into the shared `code-agent-router:local` image at `/workspace`, run the selected code agent there, then evaluate the generated patch with the official SWE-bench harness. The runner now refuses this mode unless `--allow-legacy-shared-agent-image` is passed explicitly for historical reproduction.
- `swebench-task-image`: resolve each task's official SWE-bench instance image with `swebench.harness.test_spec.make_test_spec`, build a small per-agent wrapper image on top of it, run the selected code agent inside `/testbed`, export `git diff`, then evaluate the generated patch with the official harness. This is the preferred setting for agent-vs-SWE-bench experiments.

Per-task image mode is deliberately agent-agnostic: each task resolves its own SWE-bench instance image, then the runner builds a small wrapper image by copying the selected tool layer from `--docker-image`. `claude`, `codex`, and `opencode` use separate command adapters but share the same image-resolution, artifact, timeout, and patch-export path. New agents should add one adapter branch plus any config mounts they need, not a separate SWE-bench runner.

## Run

Preflight checks Docker, the local image, Kimi Code Plan endpoint settings, and the 23-task manifest:

```sh
sh scripts/swebench_preflight.sh
```

Run one agent:

```sh
python3 scripts/swebench_run_baseline.py \
  --agent claude \
  --execution-mode swebench-task-image \
  --run-id kimi-task-image-dev23-claude-YYYYMMDD \
  --timeout 1800 \
  --skip-existing
```

Run the launcher. It defaults to `AGENTS=claude` and `EXECUTION_MODE=swebench-task-image`:

```sh
sh scripts/swebench_run_all_baseline.sh
```

Run controlled smokes for multiple adapters before any full run:

```sh
AGENTS="claude codex opencode" LIMIT=1 TIMEOUT_SECONDS=900 sh scripts/swebench_run_all_baseline.sh
```

Useful launcher knobs:

```sh
EXECUTION_MODE=swebench-task-image      # preferred; official task image plus agent tool layer
SWEBENCH_NAMESPACE=swebench             # empty string uses local SWE-bench image names
DOCKER_IMAGE=code-agent-router:local    # image containing router plus agent CLIs
FORCE_TASK_IMAGE_REBUILD=1              # rebuild wrapper images even if tags already exist
```

The full all-agent launcher is intentionally conservative. If a smoke run shows an adapter is producing only empty patches or looping, stop and inspect before spending hours on the full 23 tasks.


## Replay Prefix Runs

Replay-prefix runs reuse a previously captured clean router trace instead of calling Kimi for every attack sample. The code agent and SWE-bench task image still run for real; only the LLM response is replayed.

Collect a clean trace first:

```sh
python3 scripts/swebench_run_baseline.py \
  --agent codex \
  --execution-mode swebench-task-image \
  --run-id clean-trace-dev3-codex \
  --limit 3 \
  --timeout 1800
```

Build an optional manifest for audit/debugging:

```sh
python3 scripts/build_replay_manifest.py \
  artifacts/swebench-baseline/runs/clean-trace-dev3-codex
```

Run an append matrix against the clean trace without Kimi calls:

```sh
python3 scripts/swebench_attack_matrix.py \
  --run-id-prefix replay-prefix-dev3-turn3 \
  --agents codex \
  --samples 1:400 \
  --task-limit 3 \
  --no-baseline \
  --append \
  --attack-injection-turn 3 \
  --replay-trace-dir artifacts/swebench-baseline/runs/clean-trace-dev3-codex \
  --stop-after-attack-observed \
  --execute
```

Each replay turn writes `replay.json` next to the usual `request.agent.json`, `response.router.after.json`, and optional `injection.json`. Analysis output includes replay source paths and request hash match fields. Keep replay-prefix results separate from live-Kimi append results.

## Evaluate

The runner writes SWE-bench `predictions.jsonl`. Correctness requires the official harness:

```sh
python3 -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --split dev \
  --predictions_path artifacts/swebench-baseline/runs/<run_id>/predictions.jsonl \
  --max_workers 1 \
  --timeout 1800 \
  --run_id <run_id>-eval \
  --report_dir artifacts/swebench-baseline/runs/<run_id>/evaluation \
  --namespace swebench \
  --cache_level env \
  --clean False
```

Normalize the raw report into stable local files:

```sh
python3 scripts/swebench_collect_evaluation.py artifacts/swebench-baseline/runs/<run_id>
python3 scripts/swebench_summarize.py artifacts/swebench-baseline/runs/<run_id> --write
```

## Inspect

Concise status for one run:

```sh
python3 scripts/swebench_status.py artifacts/swebench-baseline/runs/<run_id>
```

Full machine-readable summary:

```sh
python3 scripts/swebench_summarize.py artifacts/swebench-baseline/runs/<run_id> --tail 8
```

Short human table for one or more runs:

```sh
python3 scripts/swebench_report.py artifacts/swebench-baseline/runs/<run_id>
python3 scripts/swebench_report.py artifacts/swebench-baseline/runs/* --format json
```

## Current Config Notes

- Kimi Code Plan uses model `kimi-for-coding`.
- Claude Code, Codex, and opencode all reach Kimi through the local router backed by `.env`/`KIMI_API_KEY`; there are no per-agent real Kimi keys.
- OpenAI-compatible endpoint: `https://api.kimi.com/coding/v1`.
- Anthropic-compatible endpoint: `https://api.kimi.com/coding`.
- Claude Code is currently the only agent with a completed 23-task official evaluation in this repo's artifacts.
- Codex and opencode need adapter/config work before a meaningful full 23-task score is worth running.
