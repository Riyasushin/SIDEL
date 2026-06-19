# Code Agent Router Attack Lab

这个仓库研究 code agent 在 LLM router 返回伪造 tool-call 时的行为。当前主线已经清理为三层结构：

```text
router/              共享 LLM router：鉴权即上下文路由、proxy-record、replay、replay-inject、trace、注入
swebench_container/  SWE-bench task image 容器边界：agent adapter、Docker 命令、容器 artifact
manager/             实验编排边界：manifest、router registry、结果 taxonomy
```

历史实验材料只保留在 `docs/archive/`；当前入口只描述 shared-router / SWE-bench task-container 主线。

## 当前设计

- Router 只保留 `proxy-record`、`replay`、`replay-inject`。
- API key 是 routing key：每个 SWE-bench task container 使用不同 router API key，对应一个 `ExperimentContext`。
- turn 计数按 `context_id` 隔离，多个 task container 可以共享同一个 router 进程。
- 注入不写入 prompt，而是在目标 router turn 把数据集样本转换成 OpenAI Responses、OpenAI Chat 或 Anthropic Messages 的真实 tool-call 形状。
- raw payload 只有在 manifest/context 中显式记录 `payload_execution_allowed=true` 时才渲染。
- 分析必须分开记录 `review_outcome` 和 `execution_outcome`。


## Manager CLI

生成一个最小 `proxy-record` plan：

```sh
python3 -m manager.cli plan \
  --run-id record-smoke \
  --task-limit 1 \
  --samples 1 \
  --agents codex \
  --router-mode proxy-record
```

渲染 planned container specs，不启动 Docker：

```sh
python3 -m manager.cli container-specs artifacts/runs/record-smoke \
  --router-url http://127.0.0.1:8000
```

生成数据集 profile：

```sh
python3 -m manager.cli profile-dataset \
  --output artifacts/runs/dataset_profile.json
```

汇总新 schema 的 context results：

```sh
python3 -m manager.cli analyze artifacts/runs/<run_id>
```

## Router Smoke

本地只检查 router 能启动、能解析默认上下文：

```sh
python3 - <<'PY'
from router.config import Settings
from router.context import load_context_registry
from router.app import app
settings = Settings()
context = load_context_registry(settings).contexts[0]
print(app.title, settings.router_mode, context.context_id)
PY
```

正式实验由 manager 生成 `artifacts/runs/<run_id>/router/effective_config.json`，然后把路径传给 router：

```text
ROUTER_MODE=proxy-record|replay|replay-inject
ROUTER_CONTEXT_REGISTRY=artifacts/runs/<run_id>/router/effective_config.json
KIMI_API_KEY=<kimi-code-plan-api-key>
ATTACK_ALLOW_UNSAFE_RAW=false
```

## 关键文件

- `docs/refactor-router-swebench-manager-design.md`：本轮重构的权威设计。
- `router/context.py`：API key 到 `ExperimentContext` 的解析。
- `router/app.py`：HTTP endpoints 和 proxy/replay/inject pipeline。
- `router/tracing.py`：context-scoped turn、hash、artifact 写入。
- `router/replay.py`：严格 replay request hash 校验。
- `router/injection.py`：协议感知的 append/replace 注入。
- `swebench_container/agents.py`：Codex/Claude adapter 边界。
- `swebench_container/runner.py`：Docker task container 命令构造和执行。
- `manager/manifest.py`：run manifest、context registry、API key hash。
- `manager/analysis.py`：review/execution/status taxonomy。

## 目录约定

```text
artifacts/runs/<run_id>/
  router/effective_config.json
  experiments/<context_id>/
    manifest.json
    router/traces/<turn>-<endpoint>/
    container/
    analysis/
```

`KIMI_API_KEY` 和 raw router API key 不写入 artifacts；context manifest 只保存 key hash。
