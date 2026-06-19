# SWE-bench Container Workflow

当前 SWE-bench 主线由 `manager/` 和 `swebench_container/` 承担，运行入口围绕 shared router context registry 和 task container。

## Boundary

`swebench_container/` 只负责容器运行：

- 解析或接收官方 SWE-bench task image。
- 为 Codex / Claude 构造 adapter-specific env 和 command。
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
    router.log
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

## Replay Semantics

Replay 仍然从 task 开始真实运行 agent。router 只是把 LLM response 换成历史记录；它不会伪造 Codex/Claude session 文件，也不会跳过前置 tool execution。

严格 replay 必须匹配：

```text
context_id + turn_index + endpoint + request_hash
```

hash 不匹配时，该 context 应记录为 `trace_mismatch`，不能用于正式结论。
