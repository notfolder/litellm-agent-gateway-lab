#!/usr/bin/env bash
# Claude Code と Codex の検証用の設定を生成する。利用者の普段の設定（~/.claude、~/.codex）は書き換えない。
# 生成先: clients/out/（Claude Code）、.codex-lab/（Codex の CODEX_HOME）
# どのユーザーのキーを使うかは LAB_CLIENT_USER（1 か 2、既定 1）で選ぶ。
source "$(dirname "$0")/lib.sh"
load_env
step "90 クライアントの設定"

user="${LAB_CLIENT_USER:-1}"
claude_key_var="LAB_KEY_USER${user}_CLAUDE"
codex_key_var="LAB_KEY_USER${user}_CODEX"
claude_key="${!claude_key_var:-}"
codex_key="${!codex_key_var:-}"
if ! has_users; then
  # ユーザーを作っていないときはマスターキーで動かす（試験用）
  warn "検証ユーザーが未設定なので、マスターキーで設定を作ります"
  claude_key="$LITELLM_MASTER_KEY"
  codex_key="$LITELLM_MASTER_KEY"
fi

out="$ROOT/clients/out"
codex_home="$ROOT/.codex-lab"
mkdir -p "$out" "$codex_home"
chmod 700 "$out" "$codex_home"

# ---- Claude Code ----
cat <<EOF | write_if_changed "$out/claude.env" && info "clients/out/claude.env を更新しました" || true
# clients/claude-lab が読み込む（生成物。手で編集しない）
export ANTHROPIC_BASE_URL='${LAB_BASE_URL}'
export ANTHROPIC_CUSTOM_HEADERS='x-litellm-api-key: Bearer ${claude_key}'
export LAB_LITELLM_KEY='${claude_key}'
EOF
chmod 600 "$out/claude.env"

host="localhost:${LAB_HTTP_PORT}"
jq -n --arg h "$host" '{autoMode: {environment: ["$defaults",
  ("Trusted internal domains: " + $h + "（検証用の LLM ゲートウェイ。Gmail の MCP もここ経由）")]}}' \
  | write_if_changed "$out/claude-settings.json" && info "clients/out/claude-settings.json を更新しました" || true

if has_google; then
  jq -n --arg u "${LAB_BASE_URL}/gmail/mcp" --arg k "Bearer ${claude_key}" \
    '{mcpServers: {gmail: {type: "http", url: $u, headers: {"x-litellm-api-key": $k}}}}'
else
  jq -n '{mcpServers: {}}'
fi | write_if_changed "$out/mcp.json" && info "clients/out/mcp.json を更新しました" || true
chmod 600 "$out/mcp.json"

# ---- Codex ----
codex_model="${LAB_CODEX_MODEL:-gpt-5.3-codex}"
{
  cat <<EOF
# 生成物（scripts/90-clients.sh）。手で編集しない
model = "${codex_model}"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM (lab)"
base_url = "${LAB_BASE_URL}/v1"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
EOF
  if has_google; then
    cat <<EOF

[mcp_servers.gmail]
url = "${LAB_BASE_URL}/gmail/mcp"
env_http_headers = { "x-litellm-api-key" = "LITELLM_MCP_AUTH" }
tool_timeout_sec = 180
EOF
  fi
} | write_if_changed "$codex_home/config.toml" && info ".codex-lab/config.toml を更新しました" || true

cat <<EOF | write_if_changed "$out/codex.env" && info "clients/out/codex.env を更新しました" || true
# clients/codex-lab が読み込む（生成物。手で編集しない）
export CODEX_HOME='${codex_home}'
export LITELLM_API_KEY='${codex_key}'
export LITELLM_MCP_AUTH='Bearer ${codex_key}'
EOF
chmod 600 "$out/codex.env"

ok "クライアントの設定ができました（ユーザー ${user}）: clients/claude-lab、clients/codex-lab で起動します"
has_chatgpt || warn "ChatGPT のトークンが無いので、Codex はまだ使えません（make chatgpt-login）"
