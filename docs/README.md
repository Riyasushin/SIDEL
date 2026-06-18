# 文档索引

这个目录只保留当前实验主线文档。历史验证记录和旧结果放在 `docs/archive/`，避免和当前 SWE-bench task-image 口径混在一起。

## 当前主线

- `developer-handoff.md`：给下一位修改者的详细对接文档，包含项目目标、代码路径、实验数据、replay/task-image 实现、已知坑和下一步。
- `architecture.md`：router、provider、agent、artifact 的结构说明。
- `attack-scenarios.md`：受控攻击场景和安全边界。
- `agent-attack-experiment-plan.md`：direct / append 实验设计、agent 权限模式、命令和解释规则。
- `swebench-baseline.md`：SWE-bench runner 的运行方式、task image 设置、评测和数据布局。
- `current-experiment-data-ledger.md`：当前 artifacts 总账，说明跑了什么、如何跑、主统计、启发、实现注意事项和 400 条数据集分布。
- `experiment-data-guide.md`：数据点定义、统计口径、已保存数据、缺失数据和如何查原始日志。

## 历史与参考

- `archive/codex-claude-router-verification.md`：2026-06-07 的早期 router 验证。
- `archive/claude-auto-router-attack-results.md`：2026-06-07 的 Claude auto 早期结果。
- `archive/swebench-results-20260609.md`：旧共享镜像 Claude dev23 结果，不能和当前 task-image 实验混为最终结论。
- `archive/research-report.md`、`archive/source-architecture-reading-list.md`：背景调研和阅读记录。

## 推荐阅读顺序

1. 先读 `developer-handoff.md`，理解项目现状、代码入口、已知坑和下一步。
2. 再读 `current-experiment-data-ledger.md`，理解当前已经跑出的数据、主结果和结论边界。
3. 继续读 `experiment-data-guide.md`，理解字段和统计口径。
4. 再读 `agent-attack-experiment-plan.md`，确认实验 setting。
5. 需要复现实验时看 `swebench-baseline.md`。
6. 需要改 router 或 agent 适配时看 `architecture.md`。
