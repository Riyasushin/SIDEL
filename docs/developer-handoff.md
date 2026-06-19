# Developer Handoff

更新时间：2026-06-18

当前任务是按 `docs/refactor-router-swebench-manager-design.md` 重构代码库。active code 只保留新三模块主线。

## 当前代码结构

```text
router/
  context.py      API key -> ExperimentContext
  app.py          shared router HTTP pipeline
  tracing.py      per-context turn/hash/artifacts
  replay.py       strict replay lookup
  injection.py    protocol-aware append/replace injection

swebench_container/
  agents.py       Codex/Claude adapter
  models.py       container run spec/result
  runner.py       Docker command and artifact capture

manager/
  manifest.py     run manifest + router registry
  analysis.py     review/execution/status taxonomy
```

## Mainline Router Modes

只保留：

- `proxy-record`
- `replay`
- `replay-inject`

router active modes 只保留 `proxy-record`、`replay`、`replay-inject`。

## Important Invariants

- API key 是 context routing key，不只是 bool auth。
- turn index 必须按 context 计数，不能全局共享。
- replay 正式实验必须严格校验 request hash。
- prompt 不携带 dataset sample、router mode、injection turn。
- `review_outcome` 和 `execution_outcome` 分开记录。
- raw payload 执行必须在 manifest/context 中显式记录 `payload_execution_allowed`。

## Current CLI

```sh
python3 -m manager.cli plan --run-id record-smoke --task-limit 1 --samples 1 --agents codex --router-mode proxy-record
python3 -m manager.cli container-specs artifacts/runs/record-smoke --router-url http://127.0.0.1:8000
python3 -m manager.cli profile-dataset --output artifacts/runs/dataset_profile.json
python3 -m manager.cli analyze artifacts/runs/<run_id>
```

## Next Engineering Work

- 完善 `swebench_container/` 的 official task image resolution、wrapper image build execution、patch export、Docker inspect。
- 给 `manager/` 增加真实执行命令：启动 shared router、并发调度 containers、收集 run results。
- 为 strict replay mismatch、append/replace injection、orchestrator container specs 增加更完整测试覆盖。
