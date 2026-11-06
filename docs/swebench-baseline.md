# SWE-bench Container Workflow

当前 SWE-bench 主线由 `manager/` 和 `swebench_container/` 承担，运行入口围绕 shared router context registry 和 task container。

## Boundary

`swebench_container/` 只负责容器运行：

- 解析或接收官方 SWE-bench task image。
- 为 Codex / Claude Code / OpenCode / Cursor 构造 adapter-specific env 和 command。
- 在 `/testbed` 中运行 agent。
- 保存 stdout、stderr、metadata、patch、git status 和 Docker inspect。

它不负责：

- dataset sample 选择。
- injection turn 选择。
- replay trace source 选择。
- Kimi API key。

这些属于 `manager/` 和 `router/`。

## Artifact Layout

```text
artifacts/runs/<run_id>/
  router/
    effective_config.json
    uvicorn.log
  experiments/<context_id>/
    manifest.json
    router/traces/<turn>-<endpoint>/
      metadata.json
      request.agent.json
      request.upstream.json
      response.upstream.json
      response.router.before_injection.json
      response.router.after_injection.json
      replay.json
      injection.json
      defense_scan.json
    container/
      stdout.log
      stderr.log
      metadata.json
      final.patch
      git.status.txt
    analysis/
      result.json
  analysis/
    details.csv
    summary.json
```

Claude `bypassPermissions` runs as the dedicated non-root `sidel` user inside
the task image; other modes preserve the existing task-image user. Rebuild
wrapper images after updating agent tooling (`--force-image-rebuild`).

## Replay Semantics

Replay 仍然从 task 开始真实运行 agent。router 只是把 LLM response 换成历史记录；它不会伪造 Codex/Claude session 文件，也不会跳过前置 tool execution。

正式实验可选择两种 replay 口径：

```text
exact_request_hash
task_turn_compatible
```

`exact_request_hash` 要求 endpoint 和稳定 request hash 一致。`task_turn_compatible`
忽略 title/reviewer 等辅助请求漂移，按主任务 turn 对齐，但仍会校验录制 response
引用的 tool 是否存在于当前请求。两种模式都会把匹配或失败原因写入 replay outcome。
