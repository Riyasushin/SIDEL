# Dockerized Code Agents + OpenAI/Anthropic-Compatible Router

这个实验环境把所有组件都放在 Docker 内：

- `router`：FastAPI router，支持 OpenAI Chat Completions、OpenAI Responses 和最小 Anthropic Messages 兼容入口。
- `opencode`：通过 OpenAI-compatible provider 访问 `http://router:8000/v1`。
- `codex`：通过 OpenAI Responses 访问 `http://router:8000/v1`。
- `claude`：通过 Anthropic Messages 访问 `http://router:8000`。
- `sentinel`：Docker 内部 HTTP 服务，用于记录受控 tool-call probe。

安全边界：实验 payload 只访问 Compose 网络里的 `sentinel:9000`，或在 `/workspace/.attack-lab` 写 marker 文件。

## 快速开始

构建镜像：

```sh
docker compose build
```

启动 router，默认 `ROUTER_MODE=fixed`，不需要真实模型 key：

```sh
docker compose up router
```

启动容器内 opencode：

```sh
docker compose run --rm opencode
```

默认 OpenCode 模型是 `local-router/router-fixed`，所有 fixed 请求都会返回：

```text
暂时不能给你明确的答复
```

## Smoke Test

router 启动后，在宿主机运行基础协议 smoke test：

```sh
sh scripts/smoke-test.sh
```

该脚本覆盖：

- `/v1/models`
- `/v1/chat/completions` fixed response
- `/v1/chat/completions` tool-call response
- `/v1/responses` fixed/tool-call response
- `/v1/messages/count_tokens`
- `/v1/messages` Claude-style tool_use response

## Agent Attack Lab

启动 router + sentinel，并在 Docker 内运行 Codex 与 Claude Code probe：

```sh
scripts/agent-attack-lab.sh network_probe
```

可选场景：

```text
network_probe
workspace_write_probe
env_presence_probe
compound_shell_probe
```

也可以显式传环境变量：

```sh
ATTACK_SCENARIO=workspace_write_probe ROUTER_MODE=tool_call ROUTER_PORT=18000   docker compose --profile attack-lab up -d --force-recreate router sentinel
```

Claude Code 默认权限模式会阻止 Bash。`scripts/agent-attack-lab.sh` 使用非 root 用户和 `--dangerously-skip-permissions`，只适合这个隔离 Docker lab。

## 模式

### fixed

不访问外网，也不需要上游 key。适合验证 agent 是否打到 router。

```sh
ROUTER_MODE=fixed docker compose up router
```

### proxy

根据 `model` 字段代理到 Kimi 或 DeepSeek。当前 proxy 只覆盖 OpenAI Chat Completions。

```sh
ROUTER_MODE=proxy KIMI_API_KEY=sk-... DEEPSEEK_API_KEY=sk-... docker compose up router
```

### tool_call

router 返回受控工具调用，用于验证 agent 的工具执行边界。

```sh
ROUTER_MODE=tool_call ATTACK_SCENARIO=network_probe   docker compose --profile attack-lab up router sentinel
```

## 常用环境变量

```text
ROUTER_MODE=fixed|proxy|tool_call
ROUTER_API_KEY=local-router-key
ATTACK_SCENARIO=network_probe|workspace_write_probe|env_presence_probe|compound_shell_probe
DEFAULT_PROVIDER=kimi|deepseek
FIXED_RESPONSE_TEXT=暂时不能给你明确的答复

KIMI_BASE_URL=https://api.moonshot.cn/v1
KIMI_API_KEY=
KIMI_MODEL=kimi-k2-0711-preview

DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat

OPENCODE_MODEL=local-router/router-fixed
```

## 文件结构

```text
.
├── Dockerfile
├── docker-compose.yml
├── opencode.jsonc
├── router/
├── scripts/
│   ├── agent-attack-lab.sh
│   └── smoke-test.sh
└── docs/
    ├── architecture.md
    ├── attack-scenarios.md
    ├── codex-claude-router-verification.md
    └── research-report.md
```

## 设计边界

- 完整 Claude 上游代理还未实现；当前 Anthropic Messages 入口用于 Claude Code 的受控 tool-call 验证。
- `/v1/responses` 当前为实验兼容入口；Codex fallback 请求未稳定携带预期 API key，因此该 endpoint 在 lab 内放开。
- proxy 模式不解析工具调用、reasoning 字段或多模态内容，尽量透传给上游。
- 所有攻击场景必须保持 Docker-only，并只用 sentinel 或 `/workspace/.attack-lab` 记录证据。

更多结构说明见 `docs/architecture.md`，攻击场景矩阵见 `docs/attack-scenarios.md`，网络资料调研见 `docs/research-report.md`，Codex/Claude 验证见 `docs/codex-claude-router-verification.md`。
