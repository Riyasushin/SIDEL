# Architecture

## Runtime Topology

所有实验组件都在 Docker 内：

```text
opencode container  -- OpenAI Chat Completions --> router container
codex container     -- OpenAI Responses --------> router container
claude container    -- Anthropic Messages ------> router container
                                                   |
                                                   | controlled probes
                                                   v
sentinel container  <------- Docker network -------+
```

宿主机只负责运行 `docker compose` 和读取日志。agent CLI、router、sentinel 都不依赖宿主机进程。

## Router Request Flow

`router/app.py` 提供 HTTP API：

- `GET /health`
- `GET /debug/requests`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `POST /messages`
- `POST /v1/messages/count_tokens`
- `POST /messages/count_tokens`

请求进入 router 后：

1. 读取 `model` 和 `ROUTER_MODE`。
2. `fixed` 返回固定文本。
3. `proxy` 按模型名选择 Kimi / DeepSeek provider，并透传 OpenAI Chat Completions 请求。
4. `tool_call` 根据客户端协议返回 OpenAI Chat tool_calls、OpenAI Responses function_call，或 Anthropic tool_use。
5. 如果收到 tool result，router 返回结束文本，避免无限循环。

## Modes

### fixed

不访问外网，也不需要上游 key。用于验证 agent provider 配置和 Docker 网络。

### proxy

仅代理 OpenAI Chat Completions 到 Kimi 或 DeepSeek。

代码位置：

- `router/providers/openai_compatible.py`
- `router/providers/registry.py`
- `router/models.py:model_alias_to_provider`

### tool_call

返回受控工具调用，验证 agent 的工具执行边界。`ATTACK_SCENARIO` 控制 payload：

- `network_probe`
- `workspace_write_probe`
- `env_presence_probe`
- `compound_shell_probe`

payload 生成集中在 `router/app.py:attack_command_for`。

## Protocol Compatibility

### OpenAI Chat Completions

用于 opencode。支持 fixed、proxy、tool_call，以及基础 streaming。

### OpenAI Responses

用于 Codex CLI。当前实现 fixed response、function_call 和最小 SSE events。Codex 可能先尝试 WebSocket，router 未实现 WebSocket 时会回退 HTTP streaming。

### Anthropic Messages

用于 Claude Code 的受控验证。当前实现最小 `/v1/messages`、`/messages` 和 token count stub。完整 Claude 上游代理未实现。

## Important Files

- `opencode.jsonc`：opencode provider 配置，指向 Docker 内 router。
- `Dockerfile`：安装 Python router 依赖和三个 agent CLI。
- `docker-compose.yml`：定义 router、opencode、codex、claude、sentinel。
- `router/config.py`：环境变量和默认值。
- `router/app.py`：HTTP API、模式切换、受控 payload 生成。
- `router/models.py`：OpenAI / Anthropic response 构造。
- `scripts/smoke-test.sh`：基础协议 smoke test。
- `scripts/agent-attack-lab.sh`：Docker-only agent probe harness。
