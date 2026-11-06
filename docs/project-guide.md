# SIDEL Project Guide

## Purpose

SIDEL is a security-research platform for measuring whether a third-party LLM router can change repository-level actions taken by a coding agent. The framework keeps upstream generation, router delivery, and downstream execution as separate observable artifacts.

## Repository layout

```text
manager/             experiment planning, manifests, containers, analysis
router/              shared OpenAI/Anthropic-compatible LLM router
swebench_container/  isolated coding-agent execution in task images
data/                400-record intervention dataset
bench/               SWE-bench Lite task snapshot and prompt
scripts/             curated data, diagnostic, and analysis utilities
tests/               unit and async integration tests
docs/                current guides; archive/ contains historical records
artifacts/           ignored runtime output
```

## Supported implementation

The CLI adapters support Codex over OpenAI Responses, Claude Code over Anthropic
Messages, OpenCode over Chat Completions, and Cursor over its native RPC protocol.
[Agent modes](agent-modes.md) lists the native configurations, Cursor's RPC replay
and intervention semantics, and the current validation limits.

## Installation

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

The Docker image installs current Codex and Claude Code CLIs. Local Python dependencies are pinned in `requirements.txt`.

## Configuration

### Core router

- `ROUTER_MODE`
- `ROUTER_CONTEXT_REGISTRY`
- `ROUTER_ARTIFACT_ROOT`
- `REQUEST_TIMEOUT_SECONDS`
- `REQUEST_MAX_ATTEMPTS`

### Providers

- `KIMI_BASE_URL`, `KIMI_ANTHROPIC_BASE_URL`, `KIMI_API_PROTOCOL`, `KIMI_API_KEY`, `KIMI_MODEL`
- `DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`
- `CURSOR_BASE_URL`, `CURSOR_API_KEY`: native Cursor backend and account credential, used only by the router

### Dynamic intervention

- `DYNAMIC_L3_*`: L3 backend, URL, key, model, generation controls, and retry policy
- `DYNAMIC_L4_*`: L4 URL, key, model, version, alpha, seed, token limit, and retry policy
- `CALL_VERSIONS_*`: shared fallback endpoint credentials

Real credentials belong in ignored `.env` or the process environment.

## End-to-end workflow

1. Record a clean trace with `proxy-record`.
2. Inspect that target turns contain both agent requests and router responses.
3. Plan `replay` or `replay-inject` contexts.
4. Run each context in its official SWE-bench task image.
5. Analyze review and execution evidence.
6. Aggregate DSR/WBR inputs from the analysis outputs.

Use `python -m manager.cli run` for the complete workflow, including L3/L4 service configuration and router lifecycle. The root README contains executable examples; standalone utilities are available in [scripts/](../scripts/).

## Injection selection

Dataset selectors accept:

```text
malexec-001  exact ID
1            first row
1:20         rows 1 through 20
all          complete dataset
```

Turn selection can be fixed or deterministic-random. Random mode stores the candidate turns, seed, and resolved target in each context so the experiment is reproducible.

## L1-L4 mapping

- L1: replace the response.
- L2: append a forged action to the response.
- L3: enable `--dynamic-l3` to rewrite visible assistant text.
- L4: enable `--dynamic-l4` to produce a distribution-aligned continuation.

L3/L4 never change the selected dataset tool arguments. Their outputs are validated and traced before delivery.

## Analysis invariants

- Review and execution are separate dimensions.
- Failed tool execution is still invocation evidence.
- Exact replay mismatches are not silently converted into success.
- Task-turn-compatible replay must validate live tool availability.
- Runtime credentials and artifacts are not published by default.

## Development checks

```sh
python -m unittest discover -s tests -v
python -m compileall manager router swebench_container scripts tests
python -m manager.cli profile-dataset --output /tmp/sidel-profile.json
```

Before changing an interface, update the root README, the relevant module guide, and `AGENTS.md`/`CLAUDE.md`.
