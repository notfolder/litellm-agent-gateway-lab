#!/usr/bin/env bash
# 初期データの投入: 検証ユーザーと、利用者ごとのキー（Claude Code 用、Codex 用）。
# キーの値は .env の固定値を使うので、クラスタ（DB）を作り直しても同じキーが作り直される。
# 既にあれば作らず、MCP の権限だけを揃える。
source "$(dirname "$0")/lib.sh"
load_env
step "70 初期データ"

http_ok "${LAB_BASE_URL}/health/liveliness" || die "LiteLLM に接続できません（${LAB_BASE_URL}）"

if ! has_users; then
  warn "LAB_USER1_EMAIL が未設定なので、ユーザーとキーは作りません（試験はマスターキーで行えます）"
  exit 0
fi

mcp_servers='[]'
has_google && mcp_servers='["gmail"]'

ensure_user() {
  local email="$1" role="$2"
  if admin_api GET "/user/info?user_id=$(jq -rn --arg e "$email" '$e|@uri')" | jq -e '.user_info.user_id' >/dev/null 2>&1; then
    admin_api POST /user/update "$(jq -nc --arg u "$email" --arg r "$role" '{user_id:$u, user_role:$r}')" >/dev/null
    info "ユーザー ${email} は作成済みです（役割 ${role}）"
  else
    admin_api POST /user/new "$(jq -nc --arg u "$email" --arg r "$role" \
      '{user_id:$u, user_email:$u, user_role:$r, auto_create_key:false}')" | jq -e '.user_id' >/dev/null \
      || die "ユーザー ${email} を作れませんでした"
    ok "ユーザー ${email} を作りました（役割 ${role}）"
  fi
}

ensure_key() {
  local email="$1" alias="$2" key="$3"
  local perm
  perm="$(jq -nc --argjson m "$mcp_servers" '{mcp_servers:$m}')"
  if admin_api GET "/key/info?key=${key}" | jq -e '.info.key_alias // .info.token' >/dev/null 2>&1; then
    admin_api POST /key/update "$(jq -nc --arg k "$key" --argjson p "$perm" '{key:$k, object_permission:$p}')" >/dev/null
    info "キー ${alias} は作成済みです（MCP: ${mcp_servers}）"
  else
    admin_api POST /key/generate "$(jq -nc --arg k "$key" --arg u "$email" --arg a "$alias" --argjson p "$perm" \
      '{key:$k, user_id:$u, key_alias:$a, object_permission:$p}')" | jq -e '.key' >/dev/null \
      || die "キー ${alias} を作れませんでした"
    ok "キー ${alias} を作りました（MCP: ${mcp_servers}）"
  fi
}

ensure_user "$LAB_USER1_EMAIL" proxy_admin
ensure_key "$LAB_USER1_EMAIL" user1-claude "$LAB_KEY_USER1_CLAUDE"
ensure_key "$LAB_USER1_EMAIL" user1-codex "$LAB_KEY_USER1_CODEX"

if [[ -n "${LAB_USER2_EMAIL:-}" ]]; then
  ensure_user "$LAB_USER2_EMAIL" internal_user
  ensure_key "$LAB_USER2_EMAIL" user2-claude "$LAB_KEY_USER2_CLAUDE"
  ensure_key "$LAB_USER2_EMAIL" user2-codex "$LAB_KEY_USER2_CODEX"
fi
ok "初期データの投入が終わりました"
