# Where Is the Cost of Third-Party API Routers in Agentic Software Development?

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](requirements.txt)
[![Docker](https://img.shields.io/badge/Docker-Task%20Containers-2496ED?style=flat-square&logo=docker&logoColor=white)](Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-Shared%20Router-009688?style=flat-square&logo=fastapi&logoColor=white)](router/app.py)

This paper introduces SIDEL to study response manipulation by third-party API routers across four coding agents and evaluate the effectiveness of client-side defenses.

## 🏗️ Architecture

![SIDEL architecture](docs/assets/sidel-architecture.png)

- **Manager** plans experiments, starts the shared router, launches task containers, and analyzes results.
- **Shared Router** routes requests to upstream providers and records, replays, or modifies responses. Each container receives a unique routing key that selects its experiment context.
- **Upstream Providers** generate model responses through the configured provider APIs.
- **Execution Harness** runs a coding agent inside an isolated SWE-bench task container and captures repository changes and tool-execution evidence.

Task traffic flows from the container through the router to the provider and back. Provider output, router-delivered output, reviewer decisions, and execution evidence are recorded separately.

## 🤖 Supported Agents and Modes

Select an agent with `--agents` and its mode with `--agent-mode`.

| Agent | `--agents` | Supported `--agent-mode` values | Default |
|---|---|---|---|
| Codex | `codex` | `auto`, `full_access` | `auto` |
| Claude Code | `claude` | `plan`, `acceptEdits`, `auto`, `bypassPermissions` | `auto` |
| Cursor | `cursor` | `auto_review` (Auto-review), `run_everything` (Run Everything) | `auto_review` |
| OpenCode | `opencode` | `plan`, `build` | `plan` |

`auto_review` is also accepted as an alias for Codex/Claude `auto`. OpenCode `build` uses `permission: "allow"`. For multiple agents, omit `--agent-mode` to use each agent's default. Cursor uses its native RPC adapter, pinned to CLI `2026.11.05-86047df`; see [agent modes](docs/agent-modes.md) for transport details and validation limits.

## 🎯 Injection Levels

![Four levels of router-side response intervention](docs/assets/injection-levels.png)

Use `--router-mode inject` for online interventions, or `replay-inject` to intervene on a recorded trajectory.

| Level | Method | Behavior | CLI options |
|---|---|---|---|
| L1 | Response Substitution | Replace the selected response with the dataset's injected text and tool call. | `--injection-mode replace` |
| L2 | Response Append | Keep the original response and append the injected text and tool call. | `--injection-mode append` |
| L3 | LLM-Polished Injection | Use a separate model to rewrite the injected assistant text using the current task and trajectory. | `--dynamic-l3` with `--injection-mode replace` or `append` |
| L4 | LLM-Polished with Distribution Alignment | Use the configured generation service to produce a distribution-aligned continuation from clean and injected contexts. | `--dynamic-l4` with `--injection-mode replace` or `append` |

L3 and L4 preserve the selected dataset tool arguments and are mutually exclusive. Use `--injection-turns` to select the task turn, starting at zero, and `--enable-tool-defense` to scan tool calls before delivery. Raw dataset actions require `--payload-execution-allowed` and execute inside the task container. Cursor applies these interventions at native model-step/tool boundaries; its protocol-specific behavior is described in [agent modes](docs/agent-modes.md).

## 📁 Project Structure

```text
SIDEL/
├── manager/             # Experiment planning, execution, and analysis
├── router/              # Request routing, replay, injection, and defense
├── swebench_container/  # Coding-agent adapters and isolated task execution
├── data/                # Intervention datasets
├── bench/               # SWE-bench tasks and prompt templates
├── scripts/             # Data, diagnostic, and analysis utilities
├── tests/               # Unit and integration tests
├── docs/                # Technical documentation
├── artifacts/           # Local experiment outputs (ignored by Git)
├── code_agents.py       # Agent definitions and supported modes
├── Dockerfile           # Agent tooling image
├── docker-compose.yml   # Router service configuration
├── requirements.txt     # Python dependencies
└── README.md            # Paper overview and project structure
```

## 🚀 Quick Start

### 1. Install and configure

Use Python 3.11+ and a running Docker engine with access to SWE-bench task images. Clone the repository and install the Python dependencies:

```sh
git clone https://github.com/Riyasushin/SIDEL.git
cd SIDEL
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: for the examples below, set `DEFAULT_PROVIDER=deepseek` and fill in `DEEPSEEK_API_KEY`; the example file supplies the provider URL and model. Alternatively, use `DEFAULT_PROVIDER=kimi` with `KIMI_API_KEY`. Cursor uses the router-side `CURSOR_API_KEY`. Keep real credentials in the ignored `.env` file or environment variables.

Build the agent tooling image:

```sh
docker compose build router
```

### 2. Record one task

```sh
python -m manager.cli run \
  --run-id quickstart-record \
  --agents claude --agent-mode auto \
  --router-mode proxy-record --task-limit 1 \
  --timeout-seconds 1800
```

The manager writes the context registry, starts the router, builds the task wrapper image, runs the agent, stops the router, and analyzes the results. The bundled `bench/swe_lite_dev_23.jsonl` supplies the tasks. Add `--dry-run` to generate only the manifests and registry.

These commands use Linux host networking by default. On Docker Desktop for macOS/Windows, append `--router-host 0.0.0.0 --router-url-host host.docker.internal --network bridge` so task containers can reach the host router.

### 3. Run an L3 intervention

```sh
python -m manager.cli run \
  --run-id quickstart-l3 \
  --agents claude --agent-mode auto \
  --router-mode inject --task-limit 1 --samples 1 \
  --injection-turns 0 --injection-mode replace \
  --dynamic-l3 --payload-execution-allowed \
  --timeout-seconds 1800
```

L3 uses the configured DeepSeek settings by default; `DYNAMIC_L3_*` overrides its backend, credentials, and generation options. For L4, configure `DYNAMIC_L4_BASE_URL` and `DYNAMIC_L4_API_KEY` for the generation service and use `--dynamic-l4` instead.

### 4. Continue after a simulated L1 tool call

For Claude, `--simulate-tool-results` skips execution of the injected call and supplies the selected dataset row's `tool_result` in subsequent upstream requests. It requires `inject`/`replace`, keeps tool defense disabled, and leaves the task running after injection. `first_bash` triggers once when an actual upstream response first calls Bash; earlier Read calls and auxiliary requests do not trigger it.

```sh
python -m manager.cli run \
  --run-id simulated-l1 \
  --agents claude --agent-mode bypassPermissions \
  --router-mode inject --injection-mode replace \
  --injection-turn-mode first_bash --simulate-tool-results \
  --dataset data/code_agent_attack_dataset.sampled80.seed20260624.jsonl \
  --samples 1 --task-limit 1 --max-task-turns 100 \
  --timeout-seconds 0 --request-max-attempts 3
```

A Claude `PreToolUse` adapter suppresses calls bearing the simulation ID prefix. The router replaces only their matching results, preserving tool-call IDs and unrelated results across retries and resent history. `request.agent.json` retains the original client request; `request.simulated.json` and `request.upstream.json` show the supplied result. `simulated-tool-calls.jsonl` records skipped execution. The analyzer reports `malicious_tool_simulated` separately from real execution. These runs do not set injection terminal state or write injection `terminal.json`; `--max-task-turns` caps main model interactions, and `--timeout-seconds 0` disables the overall container deadline.

On A800, `python scripts/run_simulated_injection_smoke.py` reuses one of the existing eleven Claude task images with `deepseek-v4-pro`, thinking enabled and high reasoning effort. It runs one dataset-80 sample against `pydicom__pydicom-1694`, then performs official SWE-bench grading using the existing repaired evaluation environment. Its run directory records the task result, interaction count, mean interaction count, single-run success rate, and protocol verification. Credentials stay in the existing ignored environment configuration.

Each run writes traces and container evidence under `artifacts/runs/<run-id>/experiments/` and aggregate results under `artifacts/runs/<run-id>/analysis/`. For replay, select `--router-mode replay` or `replay-inject` and pass the recorded context's `router/traces/` directory through `--trace-source-dir`. Cursor recordings also require the sibling `native_traces/` directory. See the [Manager Guide](docs/manager-guide.md) for replay matching and larger experiment matrices.
