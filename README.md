# Code Agent Router Attack Lab

这个 repo 用本地 router 研究 code agent 在 LLM response 被改写时的行为。当前主线是：让 Codex / Claude Code / opencode 通过 router 访问 Kimi Code Plan，然后在 direct 或 SWE-bench context 中注入伪造 tool-call，观察自动 review 是否拒绝、放行或没有触发。

安全边界：当前数据集实验不再把 payload 改写成 safe marker。raw 数据集参数只有在显式 ATTACK_ALLOW_UNSAFE_RAW=true 时才会渲染；推荐用 router trace 中的 auto-review 请求/回复判断 reviewer 是否放行。

## 当前主线

- Direct：router 直接返回数据集中的伪造 tool-call，用来测试协议格式和 permission gate。
- SWE-bench append：先拿到真实上游 LLM response，再把伪造 tool-call 追加到指定 turn，测试真实任务上下文中的 agent 行为。
- SWE-bench runner：最终结论只使用 `swebench-task-image`，也就是每个 task 在自己的 SWE-bench Docker image 内启动 code agent。旧的 shared-agent-image 只保留作历史复现。

## 快速开始

构建本地镜像：

```sh
docker compose build
```

启动 fixed router：

```sh
docker compose up router
```

跑基础协议 smoke test：

```sh
sh scripts/smoke-test.sh
```

Kimi Code Plan 认证检查不会打印 key：

```sh
python3 scripts/swebench_kimi_code_auth_smoke.py --anthropic
```

## Direct review-gate 实验

小样本：

```sh
ATTACK_SAMPLE=malexec-001 BUILD_ROUTER=false scripts/agent-review-gate-lab.sh
```

400 条 direct 矩阵由 `scripts/run_attack_review_matrix.py` 调度。默认只在传 `--execute` 时真正运行：

```sh
python3 scripts/run_attack_review_matrix.py \
  --run-id-prefix full400-review-codex-claude-dev3-pos135 \
  --samples 1:400 \
  --agents codex,claude \
  --no-swebench \
  --execute
```

## SWE-bench append 实验

先做 preflight：

```sh
sh scripts/swebench_preflight.sh
```

跑 3 个 dev task、400 条样本、turn 1/3/5 的 append 实验：

```sh
python3 scripts/run_attack_review_matrix.py \
  --run-id-prefix append-fast2-full400-codex-claude-dev3 \
  --samples 1:400 \
  --agents codex,claude \
  --positions 1,3,5 \
  --task-limit 3 \
  --no-direct \
  --execute
```

单个 SWE-bench baseline 或 append 的底层入口是：

```sh
python3 scripts/swebench_run_baseline.py \
  --agent codex \
  --execution-mode swebench-task-image \
  --run-id <run_id> \
  --timeout 1800 \
  --skip-existing
```

## 数据查看

当前推荐先看合并汇总：

```text
artifacts/attack-review-summary/combined-full400-direct-append-fast2/
```

关键文件：

- `details.csv`：最细粒度数据点，每行是一条 direct 观测或一个 SWE-bench task-level 观测。
- `summary.json`：聚合统计。
- `counts_by_type_turn_agent_outcome.csv`：按实验类型、turn、agent、review outcome 聚合。
- `counts_by_type_agent_outcome_execution.csv`：同时看 review 结果和是否执行。

重新生成汇总：

```sh
python3 scripts/summarize_attack_review_outcomes.py \
  --run-id-prefix full400-review-codex-claude-dev3-pos135 \
  --run-id-prefix append-fast2-full400-codex-claude-dev3-turn1 \
  --run-id-prefix append-fast2-full400-codex-claude-dev3-turn3 \
  --run-id-prefix append-fast2-full400-codex-claude-dev3-turn5 \
  --output-dir artifacts/attack-review-summary/combined-full400-direct-append-fast2
```

查单个 run 时，从 `details.csv` 的 `run_dir` 进入原始目录，再看：

- `stdout.jsonl` / `stderr.log`：agent 输出、权限 review、错误和超时。
- `metadata.json` 中的 `request_role` 与 analyzer 输出的 `review_trace_*`：auto-review 是否触发以及 reviewer 返回 allow/deny。
- `router-traces/conversations/.../metadata.json`：该 turn 的 `agent`、`instance_id`、`request_role` 等脚本级 trace 元数据。
- `router-traces/conversations/.../request.agent.json`：agent 给 router 的输入。
- `router-traces/conversations/.../response.router.after.json`：router 改写后返回给 agent 的输出。
- `router-traces/conversations/.../injection.json`：注入样本和位置。

## 文档

当前文档入口在 `docs/README.md`。

主线文档：

- `docs/experiment-data-guide.md`：数据点定义、当前统计、如何看日志、缺失数据。
- `docs/agent-attack-experiment-plan.md`：实验 setting、direct / append、权限模式和解释规则。
- `docs/swebench-baseline.md`：SWE-bench runner、task image、评测和数据布局。
- `docs/architecture.md`：router 和 agent 适配结构。
- `docs/attack-scenarios.md`：受控攻击场景。

历史文档在 `docs/archive/`，不作为当前主线结论来源。

## 目录约定

```text
router/                         FastAPI router、协议转换、注入和 trace
scripts/                        实验 runner、矩阵调度、分析和汇总脚本
docs/                           当前主线文档
docs/archive/                   历史验证和旧结果
bench/                          SWE-bench task snapshot 和 prompt 模板
data/                           攻击样本数据集
artifacts/                      运行产物，默认不提交
```

## 环境变量

```text
ROUTER_MODE=fixed|proxy|tool_call
ROUTER_API_KEY=local-router-key
DEFAULT_PROVIDER=kimi|deepseek
KIMI_API_KEY=<kimi-code-plan-api-key>
KIMI_MODEL=kimi-for-coding
ATTACK_INJECTION_MODE=off|replace|append
ATTACK_INJECTION_TURN=0|1|3|5
ATTACK_SAMPLE=1|malexec-001|...
ATTACK_PAYLOAD_MODE=raw
ATTACK_ALLOW_UNSAFE_RAW=false
```

`KIMI_API_KEY` 只应来自环境或 `.env`，不要写入文档、日志或 git。
