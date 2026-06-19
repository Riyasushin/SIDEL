# Architecture

## Topology

当前目标是一个共享 router 服务多个 SWE-bench task containers：

```text
manager
  ├─ writes router/effective_config.json
  ├─ starts shared router
  └─ starts N SWE-bench task containers

SWE-bench task container
  ├─ Codex  -> /v1/responses          -> shared router
  └─ Claude -> /v1/messages           -> shared router

shared router
  ├─ API key -> ExperimentContext
  ├─ proxy-record -> Kimi Code Plan
  ├─ replay -> recorded trace response
  └─ replay-inject -> recorded response + forged tool-call
```

## Modules

### `router/`

负责 LLM protocol proxy、协议转换、context routing、record/replay、注入和 review trace 捕获。router 不负责 Docker 生命周期，也不决定矩阵怎么枚举。

核心文件：

- `context.py`：API key 解析为 `ExperimentContext`。
- `app.py`：FastAPI endpoints，按 context 执行 proxy/replay/inject。
- `tracing.py`：per-context turn、request/response hash、artifact 写入。
- `replay.py`：严格 replay 校验。
- `injection.py`：OpenAI Responses / Chat / Anthropic Messages 注入。

### `swebench_container/`

负责官方 SWE-bench task image 中的 agent 执行边界。该模块只知道 agent、task image、router URL/API key、artifact dir 和 timeout，不知道 dataset sample 或 injection turn。

核心文件：

- `agents.py`：Codex/Claude adapter。
- `models.py`：容器运行 spec/result。
- `runner.py`：Docker command 构造和执行。

### `manager/`

负责实验计划、manifest、shared router config、context registry 和结果 taxonomy。manager 拥有 dataset/sample/turn/matrix 维度，不把这些状态塞进 prompt。

核心文件：

- `manifest.py`：生成 context manifest 和 router registry。
- `analysis.py`：`review_outcome`、`execution_outcome`、context status taxonomy。

## Router Modes

只保留三种主线模式：

- `proxy-record`：调用 Kimi，记录 agent request、upstream request/response、router response。
- `replay`：按 `context_id + turn_index + endpoint + request_hash` 返回历史 response。
- `replay-inject`：先 replay，在 `injection_turn` 按 `append` 或 `replace` 注入数据集 tool-call。

旧实验模式不再是 active code path；当前 router 只实现 `proxy-record`、`replay`、`replay-inject`。
