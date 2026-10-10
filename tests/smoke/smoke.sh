#!/usr/bin/env bash
# 疎通確認。偽 LLM（fake-tools）を使うので、ChatGPT や Claude の利用枠を使わない。
# 確かめること:
#   1. 入口から LiteLLM に届く
#   2. パススルーの経路が 403 で閉じている
#   3. 審査: 危険なツールコールは「拒否: 理由」に差し替わり、安全なものは通る（3 形式 × ストリーミング有無）
#   4. Claude Code の User-Agent の Anthropic 形式は審査されない（素通し）
#   5. 検証ユーザーのキーが使える（ユーザーを設定しているとき）
source "$(dirname "$0")/../../scripts/lib.sh"
load_env
step "疎通確認"

B="$LAB_BASE_URL"
K="$LITELLM_MASTER_KEY"
pass=0; fail=0
check() {  # check <説明> <期待: yes|no> <コマンド...>（コマンドが成功すれば yes）
  local desc="$1" expect="$2"; shift 2
  local got=no
  "$@" >/dev/null 2>&1 && got=yes
  if [[ "$got" == "$expect" ]]; then ok "OK  $desc"; pass=$((pass+1)); else warn "NG  $desc"; fail=$((fail+1)); fi
}

check "入口から LiteLLM に届く" yes http_ok "$B/health/liveliness"
for p in /openai/v1/models /anthropic/v1/messages /vertex_ai/x /bedrock/x; do
  check "パススルー ${p} は 403" yes bash -c "[[ \$(curl -s -o /dev/null -w '%{http_code}' '$B$p') == 403 ]]"
done

BAD='TOOL:shell:{\"command\":[\"git\",\"push\",\"origin\",\"--delete\",\"old-branch\"]}'
GOOD='TOOL:shell:{\"command\":[\"pytest\",\"-q\"]}'

responses() {  # responses <text> <stream>
  curl -sN --max-time 60 "$B/v1/responses" -H "Authorization: Bearer $K" -H 'content-type: application/json' \
    -H 'User-Agent: codex_cli_rs/0.162.1 (smoke)' -H 'session-id: smoke-test-0001' \
    -d "{\"model\":\"fake-tools\",\"stream\":$2,\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"$1\"}]}]}"
}
chat() {
  curl -sN --max-time 60 "$B/v1/chat/completions" -H "Authorization: Bearer $K" -H 'content-type: application/json' \
    -d "{\"model\":\"fake-tools\",\"stream\":$2,\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}"
}
messages() {  # messages <text> <stream> <user-agent>
  curl -sN --max-time 60 "$B/v1/messages" -H "x-litellm-api-key: Bearer $K" -H 'content-type: application/json' \
    -H 'anthropic-version: 2023-06-01' -H "User-Agent: $3" \
    -d "{\"model\":\"fake-tools\",\"max_tokens\":100,\"stream\":$2,\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}"
}

for stream in false true; do
  check "Responses(stream=$stream): 危険な操作は拒否に差し替わる"  yes bash -c "$(declare -f responses); B=$B K=$K; responses '$BAD' $stream | grep -q '拒否: ' && ! responses '$BAD' $stream | grep -q '\"function_call\"'"
  check "Responses(stream=$stream): 安全な操作は通る"            yes bash -c "$(declare -f responses); B=$B K=$K; responses '$GOOD' $stream | grep -q '\"function_call\"'"
  check "Chat(stream=$stream): 危険な操作は拒否に差し替わる"      yes bash -c "$(declare -f chat); B=$B K=$K; chat '$BAD' $stream | grep -q '拒否: '"
  check "Chat(stream=$stream): 安全な操作は通る"                  yes bash -c "$(declare -f chat); B=$B K=$K; chat '$GOOD' $stream | grep -q 'tool_calls'"
  check "Messages(stream=$stream, 他のクライアント): 危険な操作は拒否" yes bash -c "$(declare -f messages); B=$B K=$K; messages '$BAD' $stream 'other-agent/1.0' | grep -q '拒否: '"
  check "Messages(stream=$stream, User-Agent なし): 審査される"   yes bash -c "$(declare -f messages); B=$B K=$K; messages '$BAD' $stream '' | grep -q '拒否: '"
  check "Messages(stream=$stream, Claude Code): 審査されない"      yes bash -c "$(declare -f messages); B=$B K=$K; messages '$BAD' $stream 'claude-cli/2.1.296 (external, cli)' | grep -q 'tool_use'"
done

if has_users; then
  check "ユーザー 1 の Codex 用キーで呼べる" yes bash -c \
    "curl -fsS --max-time 30 '$B/v1/chat/completions' -H 'Authorization: Bearer $LAB_KEY_USER1_CODEX' -H 'content-type: application/json' -d '{\"model\":\"fake-tools\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
fi
if has_google; then
  check "Gmail の MCP は未サインインなら 401" yes bash -c \
    "[[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST '$B/gmail/mcp' -H 'x-litellm-api-key: Bearer ${LAB_KEY_USER1_CLAUDE:-$K}' -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}') == 401 ]]"
fi

echo >&2
if ((fail)); then die "疎通確認: ${pass} 件 OK、${fail} 件 NG"; fi
ok "疎通確認: ${pass} 件すべて OK"
