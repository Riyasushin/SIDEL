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


## 实验跑法

下面三种跑法都使用同一套目录约定：

```text
artifacts/runs/<run_id>/
  router/effective_config.json
  router/uvicorn.log
  experiments/<context_id>/
    manifest.json
    router/traces/
    container/
    analysis/result.json
  analysis/summary.json
  analysis/details.csv
  analysis/details.jsonl
```

运行前先确认 `.env` 或环境变量里有上游模型 key，例如 `KIMI_API_KEY`。`artifacts/` 默认被 `.gitignore` 忽略；只有确认有价值的数据才用 `git add -f` 入库。

### 1. 正常运行并记录 clean trace

正常记录使用 `proxy-record`：agent 走真实上游模型，router 不注入，只记录每一轮请求和响应。这个 clean trace 后续作为 replay / replay-inject 的来源。

单个 task、单个 agent 的例子：

```sh
RUN_ID=record-sqlfluff-2419-codex
INSTANCE_ID=sqlfluff__sqlfluff-2419
PORT=18080

python3 -m manager.cli plan \
  --run-id "${RUN_ID}" \
  --agents codex \
  --agent-mode auto_review \
  --router-mode proxy-record \
  --instance-id "${INSTANCE_ID}" \
  --samples 1 \
  --timeout-seconds 1800

ROUTER_CONTEXT_REGISTRY="artifacts/runs/${RUN_ID}/router/effective_config.json" \
ROUTER_ARTIFACT_ROOT=artifacts/runs \
ATTACK_DATASET_PATH=data/code_agent_attack_dataset.jsonl \
python3 -m uvicorn router.app:app --host 127.0.0.1 --port "${PORT}" \
  > "artifacts/runs/${RUN_ID}/router/uvicorn.log" 2>&1 &
ROUTER_PID=$!

python3 -m manager.cli run-containers "artifacts/runs/${RUN_ID}" \
  --router-url "http://127.0.0.1:${PORT}" \
  --build-images

kill "${ROUTER_PID}"
python3 -m manager.cli analyze "artifacts/runs/${RUN_ID}"
```

clean trace 写在：

```text
artifacts/runs/<record_run_id>/experiments/<context_id>/router/traces/
```

一个可用于严格 replay 的 trace 至少应包含对应 turn 目录下的：

```text
request.agent.json
response.router.after_injection.json
```

如果只看到 `request.agent.json`，没有 response artifact，这个 record 不能作为 replay source。

### 2. 回放到 STEP I

纯回放使用 `router_mode=replay`。含义是：agent 仍在 SWE-bench task container 里启动，但 router 不访问上游模型，而是从 clean trace 读取已记录的响应；当 `task_turn_index == STEP_I` 的响应写完后，router 标记 `replay_completed`，下一次主 agent 请求会被 409 停止。

`--trace-source-root` 的目录布局必须是：

```text
<trace_source_root>/<agent>/<safe_instance_id>/router/traces/
```

例如把上一节的 clean trace 接成 replay source：

```sh
TRACE_ROOT=artifacts/trace-sources/sqlfluff-2419-clean
AGENT=codex
INSTANCE_ID=sqlfluff__sqlfluff-2419
RECORD_CONTEXT=<record_context_id>

mkdir -p "${TRACE_ROOT}/${AGENT}/${INSTANCE_ID}/router"
ln -s "$(pwd)/artifacts/runs/record-sqlfluff-2419-codex/experiments/${RECORD_CONTEXT}/router/traces" \
  "${TRACE_ROOT}/${AGENT}/${INSTANCE_ID}/router/traces"
```

回放到 `STEP_I=3`：

```sh
RUN_ID=replay-codex-step3
INSTANCE_ID=sqlfluff__sqlfluff-2419
STEP_I=3
PORT=18081
TRACE_ROOT=artifacts/trace-sources/sqlfluff-2419-clean

python3 -m manager.cli plan \
  --run-id "${RUN_ID}" \
  --agents codex \
  --agent-mode auto_review \
  --router-mode replay \
  --instance-id "${INSTANCE_ID}" \
  --injection-turns "${STEP_I}" \
  --trace-source-root "${TRACE_ROOT}" \
  --timeout-seconds 1800

ROUTER_CONTEXT_REGISTRY="artifacts/runs/${RUN_ID}/router/effective_config.json" \
ROUTER_ARTIFACT_ROOT=artifacts/runs \
python3 -m uvicorn router.app:app --host 127.0.0.1 --port "${PORT}" \
  > "artifacts/runs/${RUN_ID}/router/uvicorn.log" 2>&1 &
ROUTER_PID=$!

python3 -m manager.cli run-containers "artifacts/runs/${RUN_ID}" \
  --router-url "http://127.0.0.1:${PORT}" \
  --build-images

kill "${ROUTER_PID}"
python3 -m manager.cli analyze "artifacts/runs/${RUN_ID}"
```

这里的 `STEP_I` 是 task turn，不是原始 HTTP 请求序号。Claude Code 的标题/bootstrap 请求不计入 task turn；第一条真正携带用户任务 prompt 的主请求是 `task_turn_index=0`。

### 3. 回放到 STEP I，并在 STEP J 注入某个数据点

注入使用 `router_mode=replay-inject`。当前 runner 里的 `--injection-step` 就是注入的 task turn，也就是这里的 `STEP_J`。实际流程是：先按 clean trace replay 到目标请求，再在该请求的响应上做 `append` 或 `replace` 注入。

数据点由 `--samples` 选择，支持：

```text
--samples buggy-001      # 一个明确样本 id
--samples 1             # 数据集第 1 行
--samples 1:20          # 数据集第 1 到 20 行
--samples all           # 全量 400 条
```

`--injection-mode append` 表示保留 clean response，再追加伪造 tool-call。`--injection-mode replace` 表示用伪造 tool-call 替换目标 response。`step0 + replace` 是特例：不需要 trace source，因为它直接覆盖第一条 task response。

单个数据点、在 `STEP_J=3` append 注入的例子：

```sh
RUN_ID=codex-step3-append-buggy-001
INSTANCE_ID=sqlfluff__sqlfluff-2419
STEP_J=3
SAMPLE=buggy-001
PORT=18082
TRACE_DIR=artifacts/runs/record-sqlfluff-2419-codex/experiments/<record_context_id>/router/traces

python3 scripts/run_codex_auto_review_matrix.py \
  --run-id "${RUN_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --samples "${SAMPLE}" \
  --injection-step "${STEP_J}" \
  --injection-mode append \
  --trace-source-dir "${TRACE_DIR}" \
  --router-port "${PORT}" \
  --timeout-seconds 1800
```

同一个数据点，改成覆盖目标响应：

```sh
python3 scripts/run_codex_auto_review_matrix.py \
  --run-id codex-step3-replace-buggy-001 \
  --instance-id sqlfluff__sqlfluff-2419 \
  --samples buggy-001 \
  --injection-step 3 \
  --injection-mode replace \
  --trace-source-dir "${TRACE_DIR}" \
  --router-port 18083 \
  --timeout-seconds 1800
```

Claude 用对应入口：

```sh
python3 scripts/run_claude_auto_review_matrix.py \
  --run-id claude-step3-append-buggy-001 \
  --instance-id sqlfluff__sqlfluff-2419 \
  --samples buggy-001 \
  --injection-step 3 \
  --injection-mode append \
  --trace-source-dir "${TRACE_DIR}" \
  --router-port 18084 \
  --timeout-seconds 1800
```

跑完整 Codex step0 replace + stepN append 可以用封装脚本：

```sh
RUN_STEP0=1 \
RUN_STEPN=1 \
STEP_N=3 \
SAMPLES=all \
TRACE_SOURCE_DIR="${TRACE_DIR}" \
TIMEOUT_SECONDS=1800 \
scripts/run_codex_step0_then_stepn_auto_matrix.sh
```

这个脚本会先检查 `TRACE_SOURCE_DIR` 里是否存在 replay response artifact；如果只有 request trace，会 fail-fast，避免再次生成无效 stepN 数据。

重跑 Codex stepN 时不要把“注入后等待 sandbox escalation / 用户提权请求”当作普通卡死直接杀掉。需要在结果里单独记录：是否请求提权、请求了什么、auto-review/用户路径如何响应。

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
