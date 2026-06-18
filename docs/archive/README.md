# Archive

这里保存历史验证、早期调试结果和背景资料。这些文件不作为当前主线实验的最终数据源。

当前主线实验要求：

- SWE-bench run 使用 `--execution-mode swebench-task-image`。
- 每个任务在对应的 SWE-bench task image 内启动 Codex / Claude Code / opencode。
- code agent 通过本地 router 访问 `.env` 中的 Kimi Code Plan 配置。
- direct 结果只用于协议和 permission gate 单元测试，不等价于真实 SWE-bench context。

归档文件说明：

- `codex-claude-router-verification.md`：早期验证 agent 能否通过 router 收到 tool-call。
- `claude-auto-router-attack-results.md`：早期 Claude auto mode 探索，只能作为背景。
- `swebench-results-20260609.md`：旧共享镜像 dev23 结果，已经重命名为 legacy 口径。
- `research-report.md`、`source-architecture-reading-list.md`：背景资料和阅读清单。
