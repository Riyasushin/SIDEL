# Current Experiment Data Ledger

> Archived point-in-time ledger. Current runs live under ignored `artifacts/`.

更新时间：2026-06-18

本文件记录 refactor 后的新口径。历史结果已经归档为背景，不再写入当前 ledger。

## Current Schema

当前结果根目录：

```text
artifacts/runs/<run_id>/
```

每个 context 必须能追溯：

- `manifest.json`：agent、agent_mode、instance_id、router_mode、dataset_sample_id、injection_turn、payload_execution_allowed。
- `router/traces/<turn>-<endpoint>/metadata.json`：context_id、turn_index、endpoint、request_role、request_hash、response_hash。
- `router/traces/<turn>-<endpoint>/injection.json`：注入样本和协议形状。
- `container/`：stdout/stderr/events/final patch/git status。
- `analysis/result.json`：status、review_outcome、execution_outcome。

## Excluded / BAD Runs

- `artifacts/runs/codex-step3-append-auto-20260619-132654/`
  - 标记文件：`BAD.json`
  - 口径：`run_quality=BAD`，`valid_for_analysis=false`，`rerun_required=true`。
  - 原因：Codex step3 append 的 replay-inject 在确认注入前失败；代表错误是缺少 replay artifact：`0029-openai.responses/response.router.after_injection.json`。
  - 数据只保留作排障证据，不纳入 Codex step3 攻击/防护结论。
  - 重跑注意：不要在注入后过早终止 Codex；需要观察它是否进入 sandbox escalation / 用户提权请求路径。

## Pending Runs

尚未在新 shared-router/task-container 架构下写入正式结果。完成 manager CLI 后，再把新 run id 和 summary 路径写入这里。
