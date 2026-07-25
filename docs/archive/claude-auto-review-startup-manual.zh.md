# Claude Auto-Review 实验启动说明书

> 历史文档：记录特定实验阶段的操作方式，不作为当前主线入口。

本文档给出一套可直接执行的端到端流程，用于：

1. 生成 `claude + auto_review` 模式的 clean trace
2. 将 clean trace 整理为 `artifacts/trace-sources/` 可复用目录
3. 基于该 clean trace 运行 `step3 + replace + 全量 400 条恶意样本` 的 injection 实验，可选开启 router tool-use 防御
4. 对实验结果执行 analysis，并说明主要输出文件与指标

本文档默认：

- 操作系统为 macOS，Docker Desktop 可用
- 容器使用 Docker `bridge` 网络
- 容器通过 `host.docker.internal` 访问宿主机上的 router
- 任务使用 `sqlfluff__sqlfluff-2419`
- agent 使用 `claude`
- review 模式使用 `auto_review`

如果你在 Linux 上运行，`host.docker.internal` 可能需要额外映射；本文档按当前仓库和 macOS 环境撰写。

## 1. 前置条件

### 1.1 安装依赖

```sh
python3 -m pip install -r requirements.txt
```

### 1.2 准备环境变量

复制环境变量模板：

```sh
cp .env.example .env
```

至少确认以下变量可用：

- `DEFAULT_PROVIDER`
- `KIMI_API_KEY` 或 `DEEPSEEK_API_KEY`
- `KIMI_BASE_URL` / `KIMI_ANTHROPIC_BASE_URL` / `KIMI_MODEL`
- `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`

说明：

- `proxy-record` 录制 clean trace 时，router 需要真的请求上游模型，因此 API Key 必须有效。
- `replay-inject` 在 `claude + auto_review` 模式下，主任务轮次可以 replay，但 reviewer 仍然可能请求上游，因此 API Key 依然需要保留。

### 1.3 启动前清理

建议每次启动前清理残留容器和端口占用：

```sh
docker ps --format '{{.Names}}' | rg '^swebench-' | xargs -r docker rm -f
```

```sh
lsof -nP -iTCP -sTCP:LISTEN | rg ':(18089|18090|18091|18092)'
```

如果某个 router 端口仍在监听，可手动结束：

```sh
kill <PID>
```

## 2. 关键概念与参数

### 2.1 Router 模式

- `proxy-record`
  向上游真实请求，并把 request/response 落盘为 trace
- `replay`
  严格回放已有 trace，不注入
- `replay-inject`
  先 replay，再在指定轮次注入恶意 tool-call
- `inject`
  在指定注入轮次之前始终真实请求上游模型；命中注入轮次时也先拿真实 response，再在该 response 上执行 `append` 或 `replace` 注入，不依赖 clean trace

### 2.2 Agent 模式

- `allow_all`
  不走审批，agent 倾向于直接执行工具
- `auto_review`
  agent 会触发 reviewer 审批链；是否执行恶意工具由 reviewer 结果控制

### 2.3 注入模式

- `append`
  在 clean response 原有内容后面追加恶意 tool-call
- `replace`
  用恶意 tool-call 替换 clean response

### 2.4 注入轮次

- `--injection-turns 3` 或脚本里的 `--injection-step 3`
- 这里的 turn 是“主任务请求轮次”，不是全局请求计数
- Claude 的 title/bootstrap 请求不计入 task turn
- 如果希望在候选轮次里随机选一个注入点，可以使用：
  - `--injection-turn-mode random`
  - `--injection-random-seed <固定整数种子>`
  - `--injection-turns 0:19` 表示只在每个 context 的前 20 个主任务轮次里随机选 1 轮注入
- 随机模式是“每个 context 基于同一个 seed 独立选出一个稳定轮次”，同一 context 重跑时结果可复现，不会每次请求重新抽样

### 2.5 Docker 网络

本文档默认使用 Docker `bridge` 网络：

- router 跑在宿主机
- 容器通过 `http://host.docker.internal:<PORT>` 访问宿主机上的 router

这也是当前仓库中最稳的 `claude + auto_review` 路径。

## 3. 恶意数据集参数说明

数据集文件默认是：

```sh
data/code_agent_attack_dataset.jsonl
```

每条样本包含这些字段：

- `id`
- `category`
- `subcategory`
- `tool_name`
- `tool_arguments`
- `model_response_text`
- `severity`
- `owasp_mapping`
- `disguised_as`
- `detection_keywords`

当前数据集规模与分布：

- 共 `400` 条
- `malicious_code_execution`: `100`
- `buggy_code_generation`: `100`
- `privacy_exfiltration`: `100`
- `supply_chain_attack`: `100`

常见工具分布：

- `Bash`: `277`
- `Write`: `74`
- `Edit`: `49`

在 `manager.cli plan` 里，`--samples` 需要传选择器，不支持脚本里的 `all` 快捷值。全量 400 条应写为：

```sh
--samples 1:400
```

## 4. 生成 Clean Trace

这里生成的是：

- agent: `claude`
- mode: `auto_review`
- router mode: `proxy-record`
- task: `sqlfluff__sqlfluff-2419`

### 4.1 规划 clean run

建议 clean trace 也先统一定义变量。这样后面的 `plan` / `uvicorn` / `run-containers` / `analyze` / `trace-sources` 整理命令都可以直接复用：

```sh
runid=record-sqlfluff-2419-claude-auto-clean-full-host-20260622-162500
instance_id=sqlfluff__sqlfluff-2419
artifacts_root=/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/artifacts/runs
router_port=18089
router_url=http://host.docker.internal:${router_port}
trace_source_name=claude-auto-clean-full-host-20260622-162500
```

```sh
python3 -m manager.cli plan \
  --run-id "${runid}" \
  --agents claude \
  --agent-mode auto_review \
  --router-mode proxy-record \
  --instance-id "${instance_id}" \
  --samples 1 \
  --timeout-seconds 1800
```

### 4.2 启动 router

建议把上游请求超时调大到 `600` 秒，避免 clean trace 在长轮次上出现 `ReadTimeout`：

```sh
env \
  ROUTER_CONTEXT_REGISTRY="${artifacts_root}/${runid}/router/effective_config.json" \
  ROUTER_ARTIFACT_ROOT="${artifacts_root}" \
  REQUEST_TIMEOUT_SECONDS=600 \
  python3 -m uvicorn router.app:app --host 0.0.0.0 --port "${router_port}"
```

说明：

- 这一步建议开一个独立终端，不要关闭

### 4.3 启动 SWE-bench 容器录制 trace

另开一个终端执行：

```sh
python3 -m manager.cli run-containers \
  "${artifacts_root}/${runid}" \
  --router-url "${router_url}" \
  --build-images
```

说明：

- `run-containers` 默认使用 Docker `bridge` 网络
- `router-url` 必须填 `host.docker.internal`
- 这样容器中的 Claude 才能访问宿主机上的 router

### 4.4 分析 clean run

录制完成后，执行：

```sh
python3 -m manager.cli analyze \
  "${artifacts_root}/${runid}"
```

这一步会：

- 在终端打印 summary
- 在 run 目录下写出 `analysis/summary.json`、`analysis/details.jsonl`、`analysis/details.csv`

### 4.5 整理为 `trace-sources`

`trace_source_root` 需要满足这个目录结构：

```text
<trace-source-root>/
  claude/
    sqlfluff__sqlfluff-2419/
      router/
        traces/
```

因此建议这样整理：

```sh
mkdir -p artifacts/trace-sources/${trace_source_name}/claude/${instance_id}
cp -R \
  "${artifacts_root}/${runid}/experiments/${runid}-${instance_id}-claude-auto_review-record-record/router" \
  "artifacts/trace-sources/${trace_source_name}/claude/${instance_id}/"
```

整理后，你的 trace source root 就是：

```sh
artifacts/trace-sources/${trace_source_name}
```

## 5. 在 Clean Trace 上运行 Injection 实验

这里运行的是：

- `claude`
- `auto_review`
- `replay-inject`
- `replace`
- `turn 3`
- `samples 1:400`

### 5.1 先定义变量

建议先统一定义本次 injection run 使用的变量。这样以后只需要改这一段，后面的命令都可以直接复用：

```sh
runid=claude-step3-replace-auto-review-all400-clean-full-host-nodef-20260622-170000
instance_id=sqlfluff__sqlfluff-2419
samples=1:400
injection_turn=3
injection_mode=replace
trace_source_name=claude-auto-clean-full-host-20260622-162500
trace_source_root=artifacts/trace-sources/${trace_source_name}
artifacts_root=/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/artifacts/runs
dataset_path=/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.jsonl
router_port=18090
router_url=http://host.docker.internal:${router_port}
replay_match_mode=task_turn_compatible
```

### 5.2 规划 injection run

```sh
python3 -m manager.cli plan \
  --run-id "${runid}" \
  --agents claude \
  --agent-mode auto_review \
  --router-mode replay-inject \
  --instance-id "${instance_id}" \
  --samples "${samples}" \
  --injection-turns "${injection_turn}" \
  --injection-mode "${injection_mode}" \
  --replay-match-mode "${replay_match_mode}" \
  --payload-execution-allowed \
  --timeout-seconds 1800 \
  --trace-source-root "${trace_source_root}"
```

如果你想固定一个随机种子，并在每个 context 的前 20 轮里随机挑 1 轮注入，可改成：

```sh
random_seed=20260624
random_turns=0:19
```

```sh
python3 -m manager.cli plan \
  --run-id "${runid}" \
  --agents claude \
  --agent-mode auto_review \
  --router-mode replay-inject \
  --instance-id "${instance_id}" \
  --samples "${samples}" \
  --injection-turn-mode random \
  --injection-random-seed "${random_seed}" \
  --injection-turns "${random_turns}" \
  --injection-mode "${injection_mode}" \
  --replay-match-mode "${replay_match_mode}" \
  --payload-execution-allowed \
  --timeout-seconds 1800 \
  --trace-source-root "${trace_source_root}"
```

说明：

- `--injection-turns 0:19` 表示候选集合是前 20 个主任务轮次
- `--injection-random-seed` 固定后，每个 context 都会稳定地选中其中一个轮次
- `replay-inject + random` 仍然需要 `--trace-source-root`，因为 router 需要先 replay clean trace，再在解析出的目标轮次执行注入

如果要开启 router 的 tool-use 防御，在 `plan` 时额外加：

```sh
  --enable-tool-defense
```

说明：

- `--payload-execution-allowed` 必须打开，否则不会按“真实执行路径”观察注入结果
- `--trace-source-root` 指向上一步整理好的 trace source 根目录
- `--replay-match-mode task_turn_compatible` 会优先按 `task_turn_index` 对齐 clean trace，更适合 injection 实验
- `--enable-tool-defense` 是一个 **plan 阶段开关**。开启后，router 会在返回 agent 前扫描每个 `tool_use` / `function_call`：
  - 命中危险命令模式时直接 deny
  - 命中域名或包名检查时，只有内置 allowlist 放行
  - deny 后会把 tool call 改写成普通警告文本，并在 trace 中写入 `defense_scan.json`

### 5.3 启动 replay-inject router

```sh
env \
  ROUTER_CONTEXT_REGISTRY="${artifacts_root}/${runid}/router/effective_config.json" \
  ROUTER_ARTIFACT_ROOT="${artifacts_root}" \
  ATTACK_DATASET_PATH="${dataset_path}" \
  REQUEST_TIMEOUT_SECONDS=600 \
  python3 -m uvicorn router.app:app --host 0.0.0.0 --port "${router_port}"
```

说明：

- `REQUEST_TIMEOUT_SECONDS=600` 对 reviewer 请求同样有帮助

### 5.4 启动 400 样本注入实验

```sh
python3 -m manager.cli run-containers \
  "${artifacts_root}/${runid}" \
  --router-url "${router_url}" \
  --build-images
```

说明：

- 当前 `manager.cli run-containers` 是顺序执行
- 如果中途被打断，建议先清理残留容器和 router，再重新跑一个全新 run-id
- 不建议直接把已污染的 run 和新结果混在一起

### 5.5 本次实际执行的 `inject + allow_all + defense` 启动脚本

这一组命令对应刚才实际跑过的实验配置：

- agent: `claude`
- agent mode: `allow_all`
- router mode: `inject`
- injection mode: `replace`
- injection turn: `5`
- samples: `1:50`
- task: `sqlfluff__sqlfluff-2419`
- defense: `on`
- payload execution: `on`
- 是否依赖 clean trace: `否`

建议先统一变量：

```sh
runid=claude-allowall-inject-step5-replace-defense50-raw-20260624
instance_id=sqlfluff__sqlfluff-2419
samples=1:400
injection_turn=5
injection_mode=replace
artifacts_root=/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/artifacts/runs
dataset_path=/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.jsonl
router_port=18093
router_url=http://host.docker.internal:${router_port}
```

规划 run：

```sh
python3 -m manager.cli plan \
  --run-id "${runid}" \
  --agents claude \
  --agent-mode allow_all \
  --router-mode inject \
  --instance-id "${instance_id}" \
  --samples "${samples}" \
  --injection-turns "${injection_turn}" \
  --injection-mode "${injection_mode}" \
  --payload-execution-allowed \
  --enable-tool-defense \
  --timeout-seconds 1800
```

启动 router：

```sh
env \
  ROUTER_CONTEXT_REGISTRY="${artifacts_root}/${runid}/router/effective_config.json" \
  ROUTER_ARTIFACT_ROOT="${artifacts_root}" \
  ATTACK_DATASET_PATH="${dataset_path}" \
  ATTACK_ALLOW_UNSAFE_RAW=true \
  REQUEST_TIMEOUT_SECONDS=600 \
  python3 -m uvicorn router.app:app --host 0.0.0.0 --port "${router_port}"
```

说明：

- 这条 `inject` 实验链路不需要 `--trace-source-root`
- `--payload-execution-allowed` 必须打开，否则第 5 轮命中注入时 raw dataset payload 不会被渲染执行
- `ATTACK_ALLOW_UNSAFE_RAW=true` 建议和上面的 plan 配置同时显式打开，避免 router 本地调试环境沿用旧默认值
- `--enable-tool-defense` 打开后，router 会为每个 turn 写 `defense_scan.json`

启动容器：

```sh
python3 -m manager.cli run-containers \
  "${artifacts_root}/${runid}" \
  --router-url "${router_url}" \
  --build-images
```

跑完后分析：

```sh
python3 -m manager.cli analyze \
  "${artifacts_root}/${runid}"
```

如果想先快速确认配置是否写对，可以先检查第一条 context：

```sh
python3 - <<'PY'
import json
from pathlib import Path
path = Path("artifacts/runs/claude-allowall-inject-step5-replace-defense50-raw-20260624/router/effective_config.json")
payload = json.loads(path.read_text())
first = next(iter(payload["contexts"].values()))
print({
    "router_mode": first["router_mode"],
    "payload_execution_allowed": first["payload_execution_allowed"],
    "enable_tool_defense": first["enable_tool_defense"],
    "injection_turn": first["injection_turn"],
})
PY
```

## 6. 对 Injection Run 执行 Analysis

实验完成后执行：

```sh
python3 -m manager.cli analyze \
  "${artifacts_root}/${runid}"
```

分析输出位于：

```text
artifacts/runs/<run_id>/analysis/
  summary.json
  details.jsonl
  details.csv
```

每个 context 还会有：

```text
artifacts/runs/<run_id>/experiments/<context_id>/analysis/result.json
```

## 7. 结果字段说明

### 7.1 `summary.json` 顶层字段

- `total`
  本次 run 一共多少个 context
- `auto_passed`
  未触发 reviewer，且恶意工具被执行的 context 数
- `reviewer_invoked`
  至少触发过 reviewer 的 context 数
- `reviewer_allowed`
  reviewer 最终给出 allow 的 context 数
- `reviewer_allowed_and_executed`
  reviewer 放行且后续观察到恶意工具执行证据的 context 数
- `by_agent`
  按 agent 分组汇总
- `by_status`
  容器层状态分布
- `by_review_outcome`
  审批结果分布
- `by_execution_outcome`
  执行结果分布
- `by_review_and_execution`
  审批结果和执行结果的联合分布

### 7.2 `review_outcome` 常见取值

- `review_not_triggered`
- `review_allowed`
- `review_denied`
- `review_unknown`
- `review_timeout`
- `review_parse_error`

### 7.3 `execution_outcome` 常见取值

- `malicious_tool_executed`
- `malicious_tool_blocked`
- `malicious_tool_not_observed`
- `execution_unknown`

### 7.4 `by_status` 常见取值

- `completed`
  agent 自己正常退出
- `payload_blocked`
  router 在后续主请求上返回 `terminal.json` / `409` 后，runner 杀掉容器
- `agent_crashed`
  容器或 agent 异常退出
- `agent_timeout`
  超时

## 8. Review 模式在 Injection 实验中的实际流程

`claude + auto_review + replay-inject` 时，主流程通常是：

1. 主 agent 在注入前按 clean trace replay
2. 第 3 轮主 response 被注入恶意 tool-call
3. router 返回注入后的 response 给 Claude
4. router 内部把该 context 标记为 `injection_completed`
5. Claude 触发 reviewer 审批
6. reviewer 给出 `block=yes` 或 `block=no`
7. Claude 本地决定是否执行恶意工具
8. 如果后续还有主 agent request，router 会在下一次主请求处返回 `409 terminal`

注意：

- `terminal` 只阻止后续主 agent 继续跑
- reviewer 请求会走 `terminal_bypass`
- 因此注入后仍可能看到多个 reviewer turn

## 9. 常见问题

### 9.1 为什么 clean trace 卡住并重复同一个请求？

常见原因是 proxy-record 到上游模型时触发了默认 `120s` 超时，Claude 会在容器内重试。建议：

```sh
REQUEST_TIMEOUT_SECONDS=600
```

### 9.2 为什么 replay 出现 request hash mismatch？

因为 replay 是严格匹配的，clean trace 与当前 agent 提示词、工具描述、审批模板必须一致。
如果 clean trace 是 `allow_all` 录出来的，而实验是 `auto_review`，很容易 mismatch。

### 9.3 为什么不推荐用脚本一键启动 `auto_review + host.docker.internal`？

`scripts/run_claude_auto_review_matrix.py` 会把：

- router 监听地址
- 容器访问 router 的地址

绑定成同一个 `--router-host`。
而我们这里需要的是：

- router 监听 `0.0.0.0`
- 容器访问 `host.docker.internal`

所以本文档推荐手动拆成：

1. `manager.cli plan`
2. 手动启动 `uvicorn`
3. `manager.cli run-containers`

### 9.4 如何停止实验？

1. 先停容器：

```sh
docker ps --format '{{.Names}}' | rg '^swebench-' | xargs -r docker rm -f
```

2. 再停 router：

```sh
kill <router_pid>
```

## 10. 推荐命名规范

### 10.1 Clean Trace Run

```text
record-<task>-claude-auto-clean-full-host-<timestamp>
```

例如：

```text
${runid}
```

### 10.2 Trace Source Root

```text
claude-auto-clean-full-host-<timestamp>
```

例如：

```text
artifacts/trace-sources/${trace_source_name}
```

### 10.3 Injection Run

```text
claude-step3-replace-auto-review-all400-clean-full-host-nodef-<timestamp>
```

这样从目录名就能直接读出：

- agent
- 注入轮次
- 注入模式
- review 模式
- 样本范围
- 是否防御
