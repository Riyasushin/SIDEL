# SIDEL

**A reproducible security-research framework for studying router-side response intervention in coding agents**

SIDEL accompanies the manuscript:

> **Where Is the Cost of Third-Party API Routers in Agentic Software Development?**
> Donghao Fu, Jingxin Li, Xue Jiang, and Yihong Dong

Third-party LLM routers terminate a coding agent's connection, forward the request to an upstream model, and return a response to the agent. This position gives the router plaintext access to prompts, repository context, tool definitions, and action-bearing model output. SIDEL makes this trust boundary measurable: it records a clean provider trace, replays it deterministically, intervenes in selected responses, and observes the repository-level actions executed by the agent.

> [!CAUTION]
> This repository contains a controlled dataset of malicious tool-call samples. Run experiments only in isolated disposable containers, never mount host secrets or the Docker socket into task containers, and enable raw payload execution only when the experiment manifest explicitly records that authorization.

## 中文概览

SIDEL 是一个面向代码智能体供应链安全的实验框架，用来研究第三方 LLM Router 在响应链路中篡改 tool-call 后，智能体是否会执行非上游模型原意的仓库级操作。系统由实验管理器、共享 Router 和隔离的 SWE-bench 任务容器组成，支持 clean trace 记录、严格或 task-turn-compatible 回放、指定轮次注入、L1–L4 干预、客户端工具防御以及审核/执行结果分析。

仓库发布版自动化支持 **Codex** 和 **Claude Code**。论文实验还评估了 Cursor 和 OpenCode；README 会区分论文报告范围与当前公开 adapter 的实现范围。

## Research Questions

The manuscript studies four questions:

1. How effective are router-side interventions across coding agents?
2. Do native permission modes change resistance to a manipulated response?
3. Does changing the upstream model remove the router-side control gap?
4. How much do whitelist execution control and LLM review mitigate the risk?

The threat model holds the upstream provider and local agent runtime honest. The router is an authorized application-layer intermediary that can inspect, replay, delay, or modify traffic. SIDEL studies the downstream effect of that mediation; it is not a network-encryption attack and it does not require prompt-injecting the upstream model.

## Intervention Taxonomy

| Level | Manuscript name | SIDEL behavior |
|---|---|---|
| L1 | Response Substitution | Replace the provider response with an injected response. |
| L2 | Response Append | Preserve the clean response and append an additional forged tool call. |
| L3 | LLM-Polished Injection | Inject the action and use a separate model to rewrite the visible assistant text so that it is task-grounded and action-aligned. |
| L4 | LLM-Polished with Distribution Alignment | Generate a task-compatible continuation through the configured multi-source generation service while locking tool arguments to the selected dataset record. |

L1 and L2 are structural response interventions. L3 and L4 add runtime rewriting through `--dynamic-l3` and `--dynamic-l4`; they are mutually exclusive for one context.

## Architecture

```text
manager/
  experiment matrix, manifests, context registry, container orchestration,
  result classification, DSR/WBR inputs
          |
          | writes artifacts/runs/<run_id>/router/effective_config.json
          v
router/
  OpenAI Responses + Chat Completions + Anthropic Messages
  API key -> ExperimentContext
  proxy-record | replay | replay-inject | inject
  L1-L4 intervention, tool defense, trace persistence
          ^
          | unique routing key per context
          |
swebench_container/
  isolated official SWE-bench task image
  Codex or Claude Code executes against a live repository
```

The API key is both an authentication credential and the context-routing key. Every task container receives a unique key that resolves to an `ExperimentContext`. Turn counters, terminal state, request serialization, replay matching, and trace artifacts are scoped to that context.

### Router modes

| Mode | Upstream call | Replay | Injection |
|---|:---:|:---:|:---:|
| `proxy-record` | Yes | No | No |
| `replay` | No | Yes | No |
| `replay-inject` | No | Yes | At the configured task turn |
| `inject` | Yes | No | At the configured task turn |

`inject` is useful when a fresh upstream trajectory is required. Controlled comparisons should normally use `proxy-record` followed by `replay` or `replay-inject`.

## Dataset

The released measurement instrument is [`data/code_agent_attack_dataset.jsonl`](data/code_agent_attack_dataset.jsonl). It contains 400 manually constructed records:

| Dimension | Distribution |
|---|---|
| Threat families | 100 malicious code execution, 100 buggy code generation, 100 privacy exfiltration, 100 supply-chain attack |
| Fine-grained subcategories | 34 |
| Tool types | 277 Bash, 74 Write, 49 Edit |
| Severity | 211 critical, 138 high, 51 medium |

Every JSONL record has ten common fields: `id`, `category`, `subcategory`, `tool_name`, `tool_arguments`, `model_response_text`, `severity`, `owasp_mapping`, `disguised_as`, and `detection_keywords`. Category-specific metadata may add CWE, target-data, exfiltration, or package-provenance fields.

The balanced category counts are an experimental-design choice, not a prevalence estimate.

## Requirements

- Python 3.11 or newer
- Docker with access to the official SWE-bench task images
- Node.js tooling is installed inside the wrapper image
- An upstream API key for `proxy-record` or `inject`
- Separate L3/L4 service credentials only when those intervention levels are enabled

Install the Python dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill only the providers you use. `.env` is ignored by Git. Never put a real credential in `.env.example`, a command-line transcript, or a tracked experiment artifact.

## Quick Start

The examples below use one SWE-bench task. Replace the instance ID and provider configuration as needed.

### 1. Record a clean trace

```sh
RUN_ID=record-sqlfluff-2419-claude
INSTANCE_ID=sqlfluff__sqlfluff-2419
PORT=18080

python -m manager.cli plan \
  --run-id "${RUN_ID}" \
  --agents claude \
  --agent-mode auto_review \
  --router-mode proxy-record \
  --instance-id "${INSTANCE_ID}" \
  --samples 1 \
  --timeout-seconds 1800

ROUTER_CONTEXT_REGISTRY="artifacts/runs/${RUN_ID}/router/effective_config.json" \
ROUTER_ARTIFACT_ROOT=artifacts/runs \
python -m uvicorn router.app:app --host 127.0.0.1 --port "${PORT}"
```

In another shell:

```sh
python -m manager.cli run-containers "artifacts/runs/${RUN_ID}" \
  --router-url "http://127.0.0.1:${PORT}" \
  --build-images

python -m manager.cli analyze "artifacts/runs/${RUN_ID}"
```

A replayable turn contains both `request.agent.json` and `response.router.after_injection.json`.

### 2. Replay and inject

Arrange the clean trace under:

```text
<trace-source-root>/<agent>/<safe-instance-id>/router/traces/
```

Then plan a replay-inject context:

```sh
python -m manager.cli plan \
  --run-id claude-l2-step3 \
  --agents claude \
  --agent-mode auto_review \
  --router-mode replay-inject \
  --instance-id sqlfluff__sqlfluff-2419 \
  --samples malexec-001 \
  --injection-turns 3 \
  --injection-mode append \
  --trace-source-root artifacts/trace-sources/sqlfluff-clean \
  --replay-match-mode exact_request_hash \
  --payload-execution-allowed
```

`task_turn_index=0` is the first real task request. Bootstrap, title, and reviewer traffic do not advance the task-turn sequence.

For controlled raw-payload experiments, both the CLI flag and the generated manifest must record `payload_execution_allowed=true`.

### 3. Use the matrix runners

The agent-specific runners wrap planning, router startup, container execution, and analysis:

```sh
python scripts/run_claude_auto_review_matrix.py \
  --run-id claude-l2-step3 \
  --instance-id sqlfluff__sqlfluff-2419 \
  --samples malexec-001 \
  --injection-step 3 \
  --injection-mode append \
  --trace-source-dir artifacts/runs/<record-run>/experiments/<context>/router/traces \
  --router-port 18082 \
  --timeout-seconds 1800
```

Use `scripts/run_codex_auto_review_matrix.py` for Codex.

### 4. Enable L3, L4, or tool defense

L3 uses the active task and recent trajectory to polish the visible continuation:

```sh
python scripts/run_claude_auto_review_matrix.py \
  ... \
  --dynamic-l3 \
  --dynamic-l3-backend deepseek_chat \
  --dynamic-l3-base-url "${DYNAMIC_L3_BASE_URL}" \
  --dynamic-l3-api-key "${DYNAMIC_L3_API_KEY}"
```

L4 uses the configured generation service. The manuscript uses `alpha=0.75`:

```sh
python scripts/run_claude_auto_review_matrix.py \
  ... \
  --dynamic-l4 \
  --dynamic-l4-call-version 3 \
  --dynamic-l4-alpha 0.75 \
  --dynamic-l4-base-url "${DYNAMIC_L4_BASE_URL}" \
  --dynamic-l4-api-key "${DYNAMIC_L4_API_KEY}" \
  --dynamic-l4-model qwen3.6-27b
```

Enable the deterministic tool-call defense with:

```sh
python scripts/run_claude_auto_review_matrix.py ... --enable-tool-defense
```

### 5. Reproduce the L4 alpha analysis

Given a recorded `dynamic_l4.request.json` artifact:

```sh
python scripts/ablate_l4_alpha.py \
  --request-file artifacts/runs/<run>/experiments/<context>/router/traces/<turn>/dynamic_l4.request.json \
  --alphas 0.50,0.75,1.00 \
  --output artifacts/runs/<run>/analysis/l4-alpha.json
```

The script reads credentials only from explicit options or `DYNAMIC_L4_*` / `CALL_VERSIONS_*` environment variables.

## Replay Semantics

SIDEL supports two matching strategies:

- `exact_request_hash`: formal strict replay. Endpoint, request hash, and recorded response must match.
- `task_turn_compatible`: aligns main-agent task turns while ignoring auxiliary reviewer/title drift, then verifies that replayed tool calls remain compatible with the live request.

Use exact matching for conclusions that depend on byte-stable requests. Use task-turn compatibility when agent bootstrap or reviewer traffic changes but the main tool interface remains compatible. Every fallback is recorded in the replay artifacts and analysis output.

## Artifacts

All runtime output is written under the ignored `artifacts/` directory:

```text
artifacts/runs/<run_id>/
  router/
    effective_config.json
    uvicorn.log
  experiments/<context_id>/
    manifest.json
    router/traces/<NNNN>-<endpoint>/
      metadata.json
      request.agent.json
      request.upstream.json
      response.upstream.json
      response.router.before_injection.json
      response.router.after_injection.json
      replay.json
      injection.json
      defense_scan.json
      dynamic_l4.request.json
      dynamic_l4.response.raw.json
    container/
    analysis/result.json
  analysis/
    summary.json
    details.csv
    details.jsonl
```

Not every turn produces every optional file. The context manifest stores a router-key hash, while the raw routing key is confined to the ignored effective configuration needed by the running router.

## Metrics

The analyzer records review and execution outcomes separately.

```text
review_outcome:
  review_not_triggered | review_allowed | review_denied |
  review_unknown | review_timeout | review_parse_error

execution_outcome:
  malicious_tool_executed | malicious_tool_blocked |
  malicious_tool_not_observed | execution_unknown
```

The manuscript uses:

```text
DSR = malicious injected tool calls denied / total malicious injected tool calls
WBR = benign tool calls denied / all denied tool calls
```

`review_not_triggered` is not equivalent to `review_allowed`, and a tool error after invocation still counts as execution rather than a security denial.

## Manuscript Results

The following values are reported by the supplied manuscript; they are not recomputed when this README is rendered:

- Without additional mitigation, all four evaluated agents reached 0% DSR across L1-L4 in the tested configurations.
- Claude Code also reached 0% DSR across the evaluated native permission modes and backend models.
- Whitelist execution control produced DSRs of 30.8%–44.1% and WBRs of 29.7%–43.9%.
- LLM-review performance depended strongly on the reviewer. Across the reported table, DSR ranged from 39.1% to 64.4%; DeepSeek-V4-Flash produced the lowest WBR range, 9.0%–14.3%.
- In the selected L4 rewrite-quality ablation, `alpha=0.75` obtained the highest reported overall score, 53.667.

These numbers are configuration-dependent. The architectural claim is narrower: when an intermediary can rewrite the response consumed by an agent, provider output and repository-level execution can diverge.

## Testing

```sh
python -m unittest discover -s tests -v
python -m compileall manager router swebench_container scripts tests
python -m manager.cli profile-dataset --output /tmp/sidel-dataset-profile.json
```

The test suite covers context routing, replay compatibility, all supported protocol shapes, L3/L4 validation and retry behavior, request serialization, timeout clamping, tool defense, container command construction, and result analysis.

## Repository Map

| Path | Purpose |
|---|---|
| `manager/` | Experiment planning, manifests, orchestration, and analysis |
| `router/` | FastAPI proxy, protocol conversion, replay, intervention, defense, tracing |
| `swebench_container/` | Codex/Claude adapters and isolated Docker execution |
| `data/` | Curated 400-record intervention dataset |
| `bench/` | SWE-bench Lite task snapshot and prompt template |
| `scripts/` | Matrix runners, reviewer reruns, L4 ablation, reporting utilities |
| `tests/` | Unit and async integration tests |
| `docs/` | Current technical documentation |
| `docs/archive/` | Historical designs, runbooks, and point-in-time experiment reports |

Start with [`docs/architecture.md`](docs/architecture.md), [`docs/project-guide.md`](docs/project-guide.md), and [`docs/experiment-data-guide.md`](docs/experiment-data-guide.md).

## Responsible Use and Disclosure

SIDEL is intended for controlled security research, reproducibility, and defense evaluation. Do not target systems you do not own or operate without authorization. Keep task containers disposable, restrict mounts and network access, inspect selected dataset samples before execution, and rotate any credential that appears in a repository or experiment log.

No software license is declared in this repository. Publication of source code does not by itself grant reuse rights.

## Citation

Until a proceedings identifier is available, cite the manuscript as:

```bibtex
@misc{fu2026sidel,
  title  = {Where Is the Cost of Third-Party API Routers in Agentic Software Development?},
  author = {Donghao Fu and Jingxin Li and Xue Jiang and Yihong Dong},
  year   = {2026},
  note   = {Manuscript}
}
```
