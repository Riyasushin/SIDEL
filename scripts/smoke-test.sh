#!/usr/bin/env sh
set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8000/v1}"
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:8000}"
ROUTER_API_KEY="${ROUTER_API_KEY:-local-router-key}"

curl -sS "$BASE_URL/models"   -H "authorization: Bearer $ROUTER_API_KEY" >/dev/null

curl -sS "$BASE_URL/chat/completions"   -H "authorization: Bearer $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-fixed","messages":[{"role":"user","content":"hello"}]}' |   grep "暂时不能给你明确的答复" >/dev/null

curl -sS "$BASE_URL/responses"   -H "authorization: Bearer $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-fixed","input":"hello"}' |   grep '"object":"response"' >/dev/null

curl -sS "$BASE_URL/chat/completions"   -H "authorization: Bearer $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-tool-call","messages":[{"role":"user","content":"hello"}],"tools":[{"type":"function","function":{"name":"run_command","description":"run shell command","parameters":{"type":"object","properties":{"command":{"type":"string"}}}}}]}' |   grep '"tool_calls"' >/dev/null

curl -sS "$BASE_URL/responses"   -H "authorization: Bearer $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-tool-call","input":"hello","tools":[{"type":"function","name":"exec_command","description":"run command","parameters":{"type":"object","properties":{"cmd":{"type":"string"}}}}]}' |   grep '"type":"function_call"' >/dev/null

curl -sS "$ANTHROPIC_BASE_URL/v1/messages/count_tokens"   -H "x-api-key: $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-claude-tool-call","messages":[{"role":"user","content":"hello"}]}' |   grep '"input_tokens"' >/dev/null

curl -sS "$ANTHROPIC_BASE_URL/v1/messages"   -H "x-api-key: $ROUTER_API_KEY"   -H "content-type: application/json"   -d '{"model":"router-claude-tool-call","max_tokens":256,"messages":[{"role":"user","content":"hello"}],"tools":[{"name":"Bash","description":"run shell command","input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}]}' |   grep '"type":"tool_use"' >/dev/null

printf "smoke test passed\n"
