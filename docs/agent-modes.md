# Native coding-agent modes

All agents run through the manager and the official CLI inside an isolated task
image. Modes are validated per agent; unknown combinations fail before execution.

| Agent (`--agents`) | `--agent-mode` | Native configuration |
|---|---|---|
| Claude Code (`claude` or `claudecode`) | `plan` | `claude --permission-mode plan --print` |
| Claude Code | `acceptEdits` | `claude --permission-mode acceptEdits --print` |
| Claude Code | `auto` | `claude --permission-mode auto --print` |
| Claude Code | `bypassPermissions` | `claude --permission-mode bypassPermissions --print`, non-root task user |
| Codex (`codex`) | `auto` | Workspace-write sandbox, on-request approvals, existing `approvals_reviewer="auto_review"` policy |
| Codex | `full_access` | `--dangerously-bypass-approvals-and-sandbox` |
| Cursor (`cursor`) | `auto_review` | `cursor-agent --auto-review --print` |
| Cursor | `run_everything` | `cursor-agent --yolo --print` |
| OpenCode (`opencode`) | `plan` | `opencode run --agent plan`, native permission defaults |
| OpenCode | `build` | `opencode run --agent build`, `permission: "allow"` |

Historical `auto_review` remains an alias for Codex/Claude `auto`. Historical
`allow_all` maps to Codex `full_access`, Claude `acceptEdits`, and Cursor
`run_everything`. In particular, Claude's old `allow_all` does not mean
`bypassPermissions`. Defaults are OpenCode `plan` and the existing `auto_review`
mode for the other agents. A mixed-agent matrix must use a mode supported by
all selected agents, or omit the mode to select each agent's default.

## Transport and scope

| Agent | Transport | Router modes |
|---|---|---|
| Codex | OpenAI Responses | All four modes; L1–L4 and tool defense |
| Claude Code | Anthropic Messages | All four modes; L1–L4 and tool defense |
| OpenCode | OpenAI Chat Completions | All four modes; L1–L4 and tool defense |
| Cursor | Native Protobuf/Connect RPC | All four modes; L1–L4 and tool defense; validation limits below |

Cursor CLI's `--endpoint` points to its own RPC API, not an OpenAI base URL.
SIDEL provides a `/cursor/` forwarding path, uses `CURSOR_API_KEY` on the router
for account authentication, gives task containers context-specific credentials,
and records native messages. The adapter decodes the pinned CLI's `RunSSE`
Connect stream and `BidiAppend` tool-result channel. Native `StepStarted` events
define task turns, starting at zero; authentication, discovery and classifier
RPCs do not consume task turns. This boundary differs from intercepting a direct
model API request and must be reported when comparing agents.

Cursor replay reads native RPC records and waits for the recorded client-message
sequence before advancing the stream. `exact_request_hash` compares complete
request bytes and rejects drift, including generated IDs. For fresh CLI sessions,
use `task_turn_compatible`: it matches RPC method/sequence, the pinned schema,
task identity, and tool-result IDs/types while permitting transport-ID and result
content drift. Missing/incomplete/incompatible records fail closed. A recording
from the earlier opaque recording adapter is not a replay source. Native
classifier and review-settings RPCs remain live during replay, so Auto-review
still needs the router's Cursor account credential.

L1/L2 deliver the selected dataset arguments and text. L3/L4 use the shared rewrite
and optimization modules, delivering text as native interaction updates and shell
descriptions. Bash and Write use native executors; Edit uses native Read followed
by Write, requiring an untruncated read and a valid literal replacement. Replace
suppresses the original text/tool at the selected step; append waits for the
original tool result before adding the injected action. The stream ends after the
injected result. New commands do not inherit upstream approval flags. Native
reviewer context and decisions remain controlled by Cursor.

Defense scans supported native tools before delivery. A denial terminates the
native stream and records the block; unsupported executable variants fail closed
when defense is enabled. Neither a tool failure nor a successful execution is
treated as reviewer approval. The adapter uses the configured HTTP/1 `RunSSE`
transport; direct HTTP/2 `Run` and `RunPoll` are rejected.

OpenCode uses an inline `@ai-sdk/openai-compatible` provider. Its API key is an
environment reference, rather than literal JSON content. Plan does not receive
permission overrides. Title generation is traced separately and does not advance
the task turn. Bash/Write/Edit values are preserved while file argument keys are
translated to OpenCode's camelCase schema.

Codex explicitly selects the router provider in both modes. Claude bypass mode
uses the wrapper image's non-root `sidel` user because the CLI refuses root
execution in this mode. Wrapper startup transfers ownership of the disposable
repository and its bound artifact directory to that user.

## Examples

```sh
python -m manager.cli run --run-id opencode-plan --agents opencode \
  --agent-mode plan --router-mode proxy-record --task-limit 1

python -m manager.cli run --run-id claude-bypass --agents claudecode \
  --agent-mode bypassPermissions --router-mode proxy-record --task-limit 1

python -m manager.cli run --run-id cursor-review --agents cursor \
  --agent-mode auto_review --router-mode proxy-record --task-limit 1
```

Configure provider credentials in ignored `.env` or the environment. Cursor
uses `CURSOR_API_KEY` and `CURSOR_BASE_URL` (default `https://api2.cursor.sh`).
For Cursor Run Everything, use `--agent-mode run_everything`.
Add `--dry-run` to inspect a plan. Rebuild the tool image with `docker compose
build` and pass `--force-image-rebuild` after changing installed CLIs.

## Source and verification basis

The mode mapping was checked on 2026-11-06 against:

- [Codex auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review) and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
- [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes).
- [Cursor CLI configuration](https://cursor.com/docs/cli/reference/configuration), [parameters](https://cursor.com/docs/cli/reference/parameters), and the official CLI `2026.11.05-86047df` help output. The binary exposes `--auto-review`, `--yolo`, and `--endpoint`; the latter is not listed in the web parameter table.
- [OpenCode agents](https://opencode.ai/docs/agents/), [permissions](https://opencode.ai/docs/permissions/), [providers](https://opencode.ai/docs/providers/), and [CLI](https://opencode.ai/docs/cli/). The tool image pins OpenCode `1.2.20`, matching the checked `permission` configuration schema.

Existing checks cover command/config generation, context planning, secret
redaction, OpenCode title-turn isolation, and Cursor authentication/stream
recording with a mock upstream. Local checks used real OpenCode Plan/Build CLIs
against a fake model and the pinned Cursor CLI against a fake native backend.
Cursor accepted an injected Write and created a harmless local marker file.
Final regression and end-to-end replay/L3/L4/defense verification remain pending.
No experiment containers were started. Live
provider access, account/model-specific reviewer behavior, and the complete
SWE-bench workflow have not been validated for this Cursor extension.
