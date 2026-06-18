# SWE-bench Results 2026-06-09

Model: `kimi-for-coding` through Kimi Code Plan. Dataset: `SWE-bench/SWE-bench_Lite`, split `dev`, 23 tasks.

## Current Bottom Line

| Agent | Generation status | Patch output | Official SWE-bench result | Interpretation |
| --- | --- | --- | --- | --- |
| Claude Code | 22 completed, 1 timeout | 22 non-empty, 1 empty | 9 resolved, 13 unresolved, 1 empty patch, 0 errors | Only complete scored baseline so far. |
| opencode | 2 timeout, 1 interrupted | 3 empty | Not run as full official eval | Current adapter/config loops without useful patches. |
| Codex | smoke timed out | 1 empty in latest smoke | Not run as full official eval | Reaches router/Kimi, but current noninteractive config does not converge. |

Claude Code resolved rate:

- Total: `9 / 23 = 39.13%`.
- Non-empty patches only: `9 / 22 = 40.91%`.

## Legacy Setting Note

This retained 2026-06-09 Claude Code run is now labeled `legacy-shared-agent-image-claude-dev23-20260609`. Its patch generation setting was:

- Claude Code ran inside the shared `code-agent-router:local` image.
- Each task repository was cloned by the host runner and mounted at `/workspace`.
- Kimi Code Plan was reached through the local router.
- Official SWE-bench task images were used only after generation, during `run_evaluation`.

That means this result is useful as a routed-agent baseline, but it is not the same setting as running the agent inside each SWE-bench task image at `/testbed`.

## Claude Code Official Evaluation

Run directory:

```text
artifacts/swebench-baseline/runs/legacy-shared-agent-image-claude-dev23-20260609
```

Key files:

```text
evaluation/report.json          raw SWE-bench harness report
evaluation/summary.json         compact score summary
evaluation/per_instance.csv     per-task agent status + SWE-bench status
summary.json                    generation + evaluation joined summary
```

Resolved tasks:

| Instance | Agent status | Patch bytes |
| --- | --- | ---: |
| `marshmallow-code__marshmallow-1343` | completed | 552 |
| `pydicom__pydicom-1256` | completed | 581 |
| `pydicom__pydicom-1694` | completed | 583 |
| `pylint-dev__astroid-1196` | completed | 995 |
| `pylint-dev__astroid-1268` | completed | 424 |
| `pylint-dev__astroid-1333` | completed | 675 |
| `pylint-dev__astroid-1866` | completed | 1890 |
| `sqlfluff__sqlfluff-1763` | completed | 3492 |
| `sqlfluff__sqlfluff-2419` | completed | 469 |

Unresolved tasks:

| Instance | Agent status | Patch bytes |
| --- | --- | ---: |
| `marshmallow-code__marshmallow-1359` | completed | 512 |
| `pvlib__pvlib-python-1072` | completed | 716 |
| `pvlib__pvlib-python-1154` | completed | 1652 |
| `pvlib__pvlib-python-1606` | completed | 15001 |
| `pvlib__pvlib-python-1707` | completed | 570 |
| `pvlib__pvlib-python-1854` | completed | 1167 |
| `pydicom__pydicom-1139` | completed | 488 |
| `pydicom__pydicom-1413` | completed | 907 |
| `pydicom__pydicom-901` | completed | 710 |
| `pylint-dev__astroid-1978` | completed | 655 |
| `pyvista__pyvista-4315` | completed | 2055 |
| `sqlfluff__sqlfluff-1517` | completed | 1089 |
| `sqlfluff__sqlfluff-1625` | completed | 1996 |

Empty patch:

| Instance | Agent status | Patch bytes |
| --- | --- | ---: |
| `sqlfluff__sqlfluff-1733` | timeout | 0 |

## opencode Evidence

Run directory:

```text
artifacts/swebench-baseline/runs/kimi-normal-dev23-opencode-20260609
```

Observed state:

- `sqlfluff__sqlfluff-1625`: timeout, empty patch.
- `sqlfluff__sqlfluff-2419`: timeout, empty patch.
- `sqlfluff__sqlfluff-1733`: interrupted after the same non-convergent pattern, empty patch.

This is not a useful official score. Running the remaining 20 tasks with the same setup would likely spend time producing more empty patches.

## Codex Evidence

Latest useful smoke directory:

```text
artifacts/swebench-baseline/runs/codex-smoke-onrequest-1
```

Observed state:

- One task timed out after 900 seconds.
- Patch output was empty.
- Router traces show the request path reached Kimi.
- Stderr included WebSocket 403 fallback noise and approval/tool execution errors under the current noninteractive `workspace-write` setup.

This should be treated as a current harness/adapter blocker, not as a 0/23 official baseline.

## Analysis

The Claude run demonstrates that the Docker-only runner, Kimi Code Plan routing, artifact capture, and official SWE-bench evaluation path are working end to end. The main gap is agent coverage: Claude has a real score, while Codex and opencode currently only have smoke evidence showing they do not produce useful patches in the current configuration.

The result files now explicitly join process status and SWE-bench correctness. That prevents the misleading interpretation that `completed` means solved.

Most useful next debugging targets:

1. Codex: decide whether to try `approval_policy=never` with `workspace-write`, or explicitly loosen the inner Codex sandbox inside the already-isolated Docker task container.
2. opencode: inspect why the JSON loop does not lead to edits, then smoke one task before any full run.
3. Claude: inspect high-risk unresolved patches, especially unusually large diffs such as `pvlib__pvlib-python-1606`, before prompt or timeout changes.
