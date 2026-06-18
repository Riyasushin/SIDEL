# Source And Architecture Reading List

Date: 2026-06-07

Purpose: understand why a local router can drive Claude Code, Codex CLI, and opencode into controlled tool execution.

This file links to source repositories, official architecture/security docs, and selected third-party architecture analyses. It does not mirror full articles locally; read the linked sources directly to avoid stale copies and copyright noise.

## Short Answer

The attack works because these tools are agent harnesses, not just chat clients. The model response is parsed as a structured tool request, then the local CLI decides whether to run it. If the CLI is configured with permissive approval/sandbox settings, a model-originated tool call becomes local shell execution.

In this repo, the router pretends to be the model API:

- Claude Code consumes an Anthropic Messages `tool_use`.
- Codex consumes an OpenAI Responses `function_call`.
- opencode consumes OpenAI Chat Completions `tool_calls` through an OpenAI-compatible provider.

## Read First

| Order | Source | Why read it |
| --- | --- | --- |
| 1 | OpenAI: Unrolling the Codex agent loop | Explains the model -> tool -> observation -> model loop. This is the core mental model for why a returned function call can become execution. |
| 2 | OpenAI: Running Codex safely | Explains the exact control points: sandbox, approvals, network policy, logs. This maps directly to why `danger-full-access` + no approvals is vulnerable. |
| 3 | Claude Code permissions docs | Shows that permissions are enforced by Claude Code, not by model instructions. Also documents `bypassPermissions`. |
| 4 | Claude Code security docs | Explains read-only defaults, Bash approval, network tool blocking, prompt-injection safeguards, and why isolated containers matter. |
| 5 | opencode agents / permissions docs | Explains provider/model routing and per-tool permissions, especially `bash`, `edit`, and MCP/custom tool rules. |

## Claude Code

### Official docs

- CLI reference: https://code.claude.com/docs/en/cli-usage
  - Read flags: `--dangerously-skip-permissions`, `--permission-mode`, `--allowedTools`, `--disallowedTools`, `--tools`, `--permission-prompt-tool`.
  - Why it matters here: our bypass run used non-root + `--dangerously-skip-permissions`, which means the CLI accepts model-originated Bash tool calls without normal prompts.

- Permissions: https://code.claude.com/docs/en/permissions
  - Read sections: permission system, permission modes, Bash rules, compound commands, network tools, hooks.
  - Key point: rules are enforced by Claude Code, not by prompts or `CLAUDE.md`.

- Security: https://code.claude.com/docs/en/security
  - Read sections: permission-based architecture, built-in protections, prompt injection, MCP security, cloud execution security.
  - Key point: Claude Code is designed to require approval for Bash by default; our attack succeeds only after we deliberately moved it into bypass mode inside Docker.

- Hooks: https://code.claude.com/docs/en/hooks
  - Read `PreToolUse` and `PermissionRequest`.
  - Why it matters: these are the right places to enforce URL/path/command policy before Bash runs.

### Architecture / analysis

- Dive into Claude Code: The Design Space of Today's and Future AI Agent Systems: https://arxiv.org/abs/2604.14228
  - Third-party architecture paper based on public TypeScript source analysis.
  - Read for the broader harness design: permission modes, context compaction, hooks, MCP, plugins, skills, subagents.

- Claude Code Permissions Model and Security Guide: https://claudecodeguides.com/claude-code-permissions-model-security-guide-2026/
  - Useful as a narrative explanation of the layered permissions architecture.
  - Treat as secondary; verify operational details against official docs.

## Codex CLI

### Source / official docs

- Source repo: https://github.com/openai/codex
  - Current repo is Rust-heavy and includes `codex-rs`, docs, SDK, and CLI surfaces.
  - Start by browsing `codex-rs` and docs around config, sandbox, approvals, and tool execution.

- Unrolling the Codex agent loop: https://openai.com/index/unrolling-the-codex-agent-loop
  - Read for the agent loop and how messages, tools, and observations fit together.
  - This explains why an OpenAI Responses `function_call` is not just text.

- Running Codex safely at OpenAI: https://openai.com/index/running-codex-safely/
  - Read sections on sandboxing, approvals, network access, rules, and telemetry.
  - Direct mapping to this repo: our lab used `--sandbox danger-full-access` and `approval_policy=never`; safe deployments should do the opposite.

- Unlocking the Codex harness: https://openai.com/index/unlocking-the-codex-harness/
  - Read for the app-server / multi-surface architecture and approval request flow.
  - Useful for understanding how Codex keeps the same agent loop across CLI, IDE, and app surfaces.

### Architecture / analysis

- OpenAI Codex CLI Architecture and Multi-Runtime Agent Patterns: https://zylos.ai/research/2026-03-26-openai-codex-cli-architecture-multi-runtime-patterns
  - Third-party architecture walkthrough.
  - Read for orchestration, tool dispatch, sandbox selection, and approval flow. Verify against source for exact names.

- Architecture teardown discussion: https://github.com/openai/codex/discussions/17051
  - Community discussion of source layout and sandbox/approval internals.
  - Useful for navigation, but not authoritative.

- Agent Safehouse Codex sandbox analysis: https://agent-safehouse.dev/docs/agent-investigations/codex
  - Security-oriented sandbox analysis.
  - Read for threat-model framing and historical version notes; confirm current behavior against current Codex release.

## opencode

### Source / official docs

- Source repo: https://github.com/anomalyco/opencode
  - Previously surfaced as `sst/opencode`; GitHub currently redirects to `anomalyco/opencode`.
  - Read source around config, providers, agents, tools, and permission handling.

- Providers docs: https://opencode.ai/docs/providers/
  - Read for custom provider setup and `@ai-sdk/openai-compatible`.
  - This is why our router can be inserted without modifying opencode source.

- Models docs: https://opencode.ai/docs/models/
  - Read for `provider/model-id` model naming.
  - This explains `local-router/router-tool-call`.

- Agents docs: https://opencode.ai/docs/agents/
  - Read permissions section: `ask`, `allow`, `deny`; tool gates like `bash`, `edit`, `webfetch`, `external_directory`, MCP wildcards.
  - This is the opencode equivalent of the control plane that should stop malicious tool calls.

### Architecture / analysis

- OpenCode env.dev overview: https://env.dev/ai/opencode
  - Good concise overview: agent harness, tool registry, provider are separated concepts.
  - Treat as secondary.

- OpenCode beginner / review articles:
  - https://nimbalyst.com/blog/what-is-opencode/
  - https://blog.openreplay.com/opencode-ai-coding-agent/
  - https://kritsana.dev/blogs/opencode-getting-started
  - Use these for high-level orientation, not exact security behavior.

## Cross-Agent Security Background

- OWASP LLM01 Prompt Injection: https://genai.owasp.org/llmrisk/llm01-prompt-injection/
  - Explains direct and indirect prompt injection.
  - Important because repo files, web pages, tool outputs, and model responses can all carry instructions.

- OWASP LLM06 Excessive Agency: https://genai.owasp.org/llmrisk/llm062025-excessive-agency/
  - Best match for this lab.
  - The vulnerability is excessive functionality + excessive permissions + excessive autonomy.

- OWASP Agentic AI Top 10 announcement: https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/
  - Read for modern agent-specific categories: tool misuse, goal hijack, identity/privilege abuse, unexpected code execution, memory/context poisoning.

## How These Explain This Repo's Result

### Claude Code path

1. `docker-compose.yml` points Claude Code at `ANTHROPIC_BASE_URL=http://router:8000`.
2. router implements `/v1/messages` and returns Anthropic `tool_use` with `name: Bash`.
3. Claude Code parses that as a real Bash tool request.
4. Default permission mode blocks it.
5. `--dangerously-skip-permissions` moves Claude into `bypassPermissions`, so the Bash command executes inside the container.

### Codex path

1. `docker-compose.yml` points Codex at `openai_base_url=http://router:8000/v1`.
2. router implements `/v1/responses` and returns OpenAI Responses `function_call`.
3. Codex parses that as a command-execution request.
4. The lab config uses `--sandbox danger-full-access` and `approval_policy=never`.
5. Codex runs the command inside the container.

### opencode path

1. `opencode.jsonc` defines a custom `@ai-sdk/openai-compatible` provider.
2. `ROUTER_BASE_URL=http://router:8000/v1` points opencode at the router.
3. router returns OpenAI Chat Completions `tool_calls`.
4. If opencode permissions allow the matching tool, the harness can execute it.

## What To Look For In Source Code

When reading source, search these concepts rather than exact filenames first:

- `tool_use`, `tool_calls`, `function_call`
- `Bash`, `shell`, `exec`, `command`
- `approval`, `permission`, `bypass`, `sandbox`
- `provider`, `base_url`, `baseURL`, `model`
- `PreToolUse`, `PermissionRequest`, `MCP`
- `workspace`, `writable_roots`, `network`, `egress`

The key source-code question is always: where does a structured model response become a local side effect?
