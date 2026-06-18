# SWE-bench Lite Dev Baseline

This directory holds the reproducible task snapshot and prompt template for the Kimi-backed normal-mode baseline. Runtime artifacts are written under `artifacts/swebench-baseline/` and are intentionally ignored by git. The host runner orchestrates Docker, stores prompts/artifacts, and collects patches. The default execution mode runs each task inside its official SWE-bench instance image, with the selected code-agent CLI layered in automatically.

Detailed workflow and current result analysis live in `docs/swebench-baseline.md` and `docs/swebench-results-20260609.md`.

## Data model

A run directory is created as:

```text
artifacts/swebench-baseline/runs/<run_id>/
├── run.json
├── tasks.jsonl
├── predictions.jsonl              # combined rows, useful for inspection
├── summary.json
├── router-traces/
└── agents/<agent>/
    ├── predictions.jsonl          # per-agent SWE-bench prediction file
    └── <instance_id>/
        ├── metadata.json
        ├── prompt.md
        ├── stdout.log or stdout.jsonl
        ├── stderr.log
        ├── final.patch
        ├── git.diff
        └── exit.json
```

`KIMI_API_KEY` is read from the process environment or from `--env-file` (default `.env` when present). Do not commit real keys; `.env` is ignored by git. The runner passes this key only to the local router container. Claude Code, Codex, and opencode receive `local-router-key` plus a router base URL, so all three agents use the same Kimi Code Plan backend without seeing the real key.

## Workflow

1. Fetch the fixed 23-task dev split snapshot:

```sh
python3 scripts/swebench_fetch_tasks.py
```

2. Build the Docker image used by the router and all agent containers:

```sh
docker compose build
```

3. Dry-run one task in the preferred per-task image mode. This resolves the official SWE-bench image and writes the generated Docker command, but does not start Docker, an agent, or Kimi:

```sh
python3 scripts/swebench_run_baseline.py \
  --agent claude \
  --execution-mode swebench-task-image \
  --limit 1 \
  --dry-run
```

4. Run a one-task smoke baseline with Kimi available in the environment. The key is inherited only by the router container; it is not serialized to artifacts:

```sh
# Either export KIMI_API_KEY first, or put KIMI_API_KEY=... in ignored .env.
python3 scripts/swebench_run_baseline.py \
  --agent claude \
  --execution-mode swebench-task-image \
  --limit 1
```

5. Run the full baseline after the one-task trace shape is verified:

```sh
# Either export KIMI_API_KEY first, or put KIMI_API_KEY=... in ignored .env.
python3 scripts/swebench_run_baseline.py --agent claude --execution-mode swebench-task-image
python3 scripts/swebench_run_baseline.py --agent codex --execution-mode swebench-task-image
python3 scripts/swebench_run_baseline.py --agent opencode --execution-mode swebench-task-image
```

Or use the launcher, which creates one run directory per agent and writes summaries. It defaults to `AGENTS=claude`, `EXECUTION_MODE=swebench-task-image`, `SWEBENCH_NAMESPACE=swebench`; pass `AGENTS`, `LIMIT`, or `FORCE_TASK_IMAGE_REBUILD=1` for smoke/debug runs:

```sh
# Either export KIMI_API_KEY first, or put KIMI_API_KEY=... in ignored .env.
sh scripts/swebench_run_all_baseline.sh
AGENTS="claude codex opencode" LIMIT=1 TIMEOUT_SECONDS=900 sh scripts/swebench_run_all_baseline.sh
```

The runner records patches in SWE-bench `predictions.jsonl` shape at both run level and `agents/<agent>/predictions.jsonl` for later official evaluation.

## Evaluation

Agent `completed` only means the CLI exited and a patch was collected. It is not the SWE-bench score. Official correctness is produced by the SWE-bench harness from `predictions.jsonl`:

```sh
python3 -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --split dev \
  --predictions_path artifacts/swebench-baseline/runs/<run_id>/predictions.jsonl \
  --max_workers 1 \
  --timeout 1800 \
  --run_id <run_id>-eval \
  --report_dir artifacts/swebench-baseline/runs/<run_id>/evaluation
```

Evaluation artifacts should be stored under `artifacts/swebench-baseline/runs/<run_id>/evaluation/`:

```text
evaluation/
├── report.json              # raw SWE-bench harness report
├── summary.json             # compact resolved/unresolved/empty/error counts
├── per_instance.jsonl       # per-task status joined with generation metadata
└── per_instance.csv
```

After the harness writes a raw report, normalize it into the run directory and update the run summary:

```sh
python3 scripts/swebench_collect_evaluation.py \
  artifacts/swebench-baseline/runs/<run_id> \
  --report artifacts/swebench-baseline/runs/<run_id>/evaluation/report.json

python3 scripts/swebench_summarize.py artifacts/swebench-baseline/runs/<run_id> --write
```

Current Claude baseline example: `legacy-shared-agent-image-claude-dev23-20260609` generated 23 predictions; official harness result is 9 resolved, 13 unresolved, 1 empty patch, 0 errors.
