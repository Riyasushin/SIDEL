## Claude `inject` 实验 `tmux` 启动模板

> 历史文档：记录特定实验阶段的操作方式，不作为当前主线入口。

这份文档沉淀的是一个**可重复使用**的 `tmux` 双 pane 启动方式，适合你后续反复改配置跑 Claude 注入实验。

特点：

- 统一使用 `python`
- pane 0 跑 `router`
- pane 1 跑 `run-containers`，结束后自动 `analyze`
- 只改顶部变量，就能切换：
  - `allow_all` / `auto_review`
  - `defense on` / `off`
  - `80` 条子集 / `400` 条全量
  - `random 0:24` / 其他注入轮次

## 1. 你通常只需要改这些变量

```sh
cd /Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack

STAMP="$(date +%Y%m%d-%H%M%S)"

# 核心实验配置
AGENT_MODE="allow_all"          # allow_all 或 auto_review
ENABLE_TOOL_DEFENSE="1"         # 1=开启，0=关闭
RUN_TAG="all400-defense"        # 给 run_id / tmux session 起一个好认的标签

# 数据集配置
DATASET_PATH="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.jsonl"
SAMPLES="1:400"                 # 全量 400 条；如果是 80 条子集可改成 1:80

# 注入配置
INJECTION_MODE="replace"
INJECTION_TURN_MODE="random"
INJECTION_RANDOM_SEED="20260624"
INJECTION_TURNS="0:24"

# 固定任务与运行环境
INSTANCE_ID="sqlfluff__sqlfluff-2419"
ARTIFACTS_ROOT="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/artifacts/runs"
ROUTER_PORT="18093"
ROUTER_URL="http://host.docker.internal:${ROUTER_PORT}"
TIMEOUT_SECONDS="1800"

RUN_ID="claude-${AGENT_MODE}-inject-random25-${INJECTION_MODE}-${RUN_TAG}-${STAMP}"
TMUX_SESSION="claude-${AGENT_MODE}-${RUN_TAG}-${STAMP}"
```

## 2. 通用 `tmux` 启动脚本

直接整段执行即可。它会：

1. 清理旧 router / 旧容器 / 旧 `tmux` session
2. `plan`
3. 新建 `tmux` session
4. pane 0 启动 router
5. pane 1 等待 router 健康后跑容器和分析

```sh
pkill -f 'python -m manager.cli run-containers' || true
pkill -f "python -m uvicorn router.app:app --host 0.0.0.0 --port ${ROUTER_PORT}" || true

pids="$(lsof -tiTCP:${ROUTER_PORT} -sTCP:LISTEN 2>/dev/null || true)"
[ -z "${pids}" ] || kill -9 ${pids}

container_ids="$(docker ps -aq --filter 'name=swebench-' || true)"
[ -z "${container_ids}" ] || docker rm -f ${container_ids}

tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true

DEFENSE_FLAG=""
if [ "${ENABLE_TOOL_DEFENSE}" = "1" ]; then
  DEFENSE_FLAG="--enable-tool-defense"
fi

python -m manager.cli plan \
  --run-id "${RUN_ID}" \
  --agents claude \
  --agent-mode "${AGENT_MODE}" \
  --router-mode inject \
  --instance-id "${INSTANCE_ID}" \
  --dataset "${DATASET_PATH}" \
  --samples "${SAMPLES}" \
  --injection-turn-mode "${INJECTION_TURN_MODE}" \
  --injection-random-seed "${INJECTION_RANDOM_SEED}" \
  --injection-turns "${INJECTION_TURNS}" \
  --injection-mode "${INJECTION_MODE}" \
  --payload-execution-allowed \
  ${DEFENSE_FLAG} \
  --timeout-seconds "${TIMEOUT_SECONDS}"

tmux new-session -d -s "${TMUX_SESSION}" -c /Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack

tmux send-keys -t "${TMUX_SESSION}:0.0" \
"env ROUTER_CONTEXT_REGISTRY='${ARTIFACTS_ROOT}/${RUN_ID}/router/effective_config.json' \
ROUTER_ARTIFACT_ROOT='${ARTIFACTS_ROOT}' \
ATTACK_DATASET_PATH='${DATASET_PATH}' \
ATTACK_ALLOW_UNSAFE_RAW=true \
REQUEST_TIMEOUT_SECONDS=600 \
python -m uvicorn router.app:app --host 0.0.0.0 --port '${ROUTER_PORT}' \
| tee '${ARTIFACTS_ROOT}/${RUN_ID}/router/uvicorn.log'" C-m

tmux split-window -h -t "${TMUX_SESSION}:0" -c /Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack

tmux send-keys -t "${TMUX_SESSION}:0.1" \
"until curl -sf 'http://127.0.0.1:${ROUTER_PORT}/health' >/dev/null; do sleep 1; done; \
python -m manager.cli run-containers '${ARTIFACTS_ROOT}/${RUN_ID}' --router-url '${ROUTER_URL}' --build-images && \
python -m manager.cli analyze '${ARTIFACTS_ROOT}/${RUN_ID}'" C-m

printf 'RUN_ID=%s\nTMUX_SESSION=%s\nRUN_ROOT=%s\n' \
  "${RUN_ID}" \
  "${TMUX_SESSION}" \
  "${ARTIFACTS_ROOT}/${RUN_ID}"
```

## 3. 查看与接管现场

```sh
tmux attach -t "${TMUX_SESSION}"
```

常用查看命令：

```sh
tmux list-panes -t "${TMUX_SESSION}" -F '#{pane_index} #{pane_dead} #{pane_current_command}'
tmux capture-pane -pt "${TMUX_SESSION}:0.0" | tail -n 30
tmux capture-pane -pt "${TMUX_SESSION}:0.1" | tail -n 40
```

## 4. 如何切换常见实验配置

### 4.1 `auto_review` + 80 条子集 + defense off

```sh
AGENT_MODE="auto_review"
ENABLE_TOOL_DEFENSE="0"
RUN_TAG="sampled80-nodefense"
DATASET_PATH="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.sampled80.seed20260624.jsonl"
SAMPLES="1:80"
```

### 4.2 `auto_review` + 80 条子集 + defense on

```sh
AGENT_MODE="auto_review"
ENABLE_TOOL_DEFENSE="1"
RUN_TAG="sampled80-defense"
DATASET_PATH="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.sampled80.seed20260624.jsonl"
SAMPLES="1:80"
```

### 4.3 `allow_all` + 全量 400 条 + defense on

```sh
AGENT_MODE="allow_all"
ENABLE_TOOL_DEFENSE="1"
RUN_TAG="all400-defense"
DATASET_PATH="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.jsonl"
SAMPLES="1:400"
```

### 4.4 `allow_all` + 全量 400 条 + defense off

```sh
AGENT_MODE="allow_all"
ENABLE_TOOL_DEFENSE="0"
RUN_TAG="all400-nodefense"
DATASET_PATH="/Users/Zhuanz/Documents/CodeAgentAttack-Man-in-the-Middle-Attack/data/code_agent_attack_dataset.jsonl"
SAMPLES="1:400"
```

## 5. 实验结束后看哪里

主输出目录：

```text
artifacts/runs/${RUN_ID}/
```

重点看这几个文件：

- `${RUN_ROOT}/router/uvicorn.log`
- `${RUN_ROOT}/analysis/summary.json`
- `${RUN_ROOT}/analysis/details.csv`
- `${RUN_ROOT}/experiments/<context_id>/router/traces/*/injection.json`
- `${RUN_ROOT}/experiments/<context_id>/router/traces/*/defense_scan.json`

## 6. 两个容易踩的坑

### 6.1 `malicious_tool_defense_allowed + malicious_tool_defense_denied != total`

这是正常的。`total` 是计划出来的 context 数；`malicious_tool_defense_*` 统计的是**实际注入并被 defense 扫描到的恶意工具条目数**。如果随机目标 turn 没跑到，就不会有注入，也不会进入这两个计数。

### 6.2 `random 0:24` 会有一部分样本根本没注入

也是正常的。因为有些 context 在走到第 24 个 task turn 之前就已经完成了。想减少这种情况，可以把：

```sh
INJECTION_TURNS="0:24"
```

收窄成例如：

```sh
INJECTION_TURNS="0:12"
```

或：

```sh
INJECTION_TURNS="0:15"
```
