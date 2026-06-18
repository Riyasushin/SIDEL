# Research Report: Dockerized Code Agents + Local Router

调研日期：2026-06-07

## Scope

本次调研覆盖当前仓库的实际形态：opencode、Codex CLI、Claude Code、router、sentinel 全部运行在 Docker 内。目标是确认本地 router 能否同时模拟 OpenAI-compatible 与 Anthropic-compatible 响应，并在受控实验中触发 code agent 的工具调用。

## Sources Reviewed

- OpenCode Providers / Models 文档：自定义 provider 可使用 OpenAI-compatible `baseURL`，模型格式为 `provider_id/model_id`。  
  https://opencode.ai/docs/providers/  
  https://opencode.ai/docs/models/
- Docker 官方 OpenCode + Docker Model Runner 指南：建议先验证 endpoint，再启动 OpenCode。  
  https://docs.docker.com/guides/opencode-model-runner/
- OWASP LLM01:2025 Prompt Injection：直接/间接 prompt injection 可影响工具调用和连接系统行为。  
  https://genai.owasp.org/llmrisk/llm01-prompt-injection/
- OWASP LLM06:2025 Excessive Agency：风险根因包括 excessive functionality、permissions、autonomy。  
  https://genai.owasp.org/llmrisk/llm062025-excessive-agency/
- Claude Code permissions 文档：权限由 Claude Code 执行层强制，提示词不能改变真实 allow/deny 边界。  
  https://code.claude.com/docs/en/permissions
- OpenAI Codex 安全说明：sandbox 定义执行边界，approval policy 决定何时需要批准。  
  https://openai.com/index/running-codex-safely/

## Current Findings

### 1. opencode 不需要改源码

`opencode.jsonc` 使用 `@ai-sdk/openai-compatible` 和 `ROUTER_BASE_URL=http://router:8000/v1`。模型 ID 使用 `local-router/router-fixed` 或 `local-router/router-tool-call`，符合 OpenCode provider/model 规则。

### 2. Codex 走 OpenAI Responses

Codex CLI 当前通过 `/v1/responses` 与 router 交互。实验中 Codex 先尝试 Responses WebSocket，router 未实现 WebSocket 时回落到 HTTP streaming。router 已实现最小 Responses object、function_call、streaming event，用于受控 tool-call 验证。

### 3. Claude Code 走 Anthropic Messages

router 已实现最小 `/v1/messages`、`/messages`、`/v1/messages/count_tokens`、`/messages/count_tokens`。该实现足够验证 Claude Code 是否接收 `tool_use`，以及默认权限模式和 bypass 模式的差异。完整 Claude 上游代理仍未实现。

### 4. 攻击面应聚焦 agentic tool boundary

结合 OWASP LLM01、LLM06 和 Agentic Top 10，本仓库最适合测试的不是传统模型 jailbreak，而是：模型输出如何穿过 agent 的工具选择、权限审批、shell 执行、容器网络和 workspace mount 边界。

已集成的安全场景在 `docs/attack-scenarios.md` 中维护：`network_probe`、`workspace_write_probe`、`env_presence_probe`、`compound_shell_probe`。

## Impact On This Repository

- `Dockerfile` 安装 `opencode-ai`、`@openai/codex`、`@anthropic-ai/claude-code`，保证 agent CLI 均在镜像内。
- `docker-compose.yml` 定义 `router`、`opencode`、`codex`、`claude`、`sentinel`，并通过 `ATTACK_SCENARIO` 控制受控 payload。
- `router/app.py` 支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 三类入口。
- `scripts/smoke-test.sh` 覆盖 fixed、tool_call、Responses、Anthropic count_tokens/messages。
- `scripts/agent-attack-lab.sh` 用 Compose 启动 router/sentinel，并在 Docker 内运行 Codex 与 Claude bypass probe。

## Risks And Follow-Ups

- `latest` npm 包会带来可重复性风险；后续应在 Dockerfile 固定 opencode/Codex/Claude Code 版本。
- `/v1/responses` 当前为 lab-open，因为当前 Codex fallback 请求未带预期 API key；如果用于非实验环境，需要补齐认证兼容性或限制网络入口。
- Claude 默认权限模式会阻止 Bash，bypass 模式必须只在隔离 Docker lab 使用。
- 间接 prompt injection、tool metadata poisoning、memory/context poisoning 还未做自动化夹具；建议单独加 Compose profile 和可清理 fixture。
