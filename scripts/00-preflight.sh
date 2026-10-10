#!/usr/bin/env bash
# 前提の確認。何も変更しない（.env が無いときに雛形からコピーすることだけを行う）。
source "$(dirname "$0")/lib.sh"
step "00 前提の確認"

for cmd in docker k3d kubectl helm jq envsubst openssl curl python3; do
  command -v "$cmd" >/dev/null || die "$cmd が見つかりません（brew install で入れてください）"
done

docker info >/dev/null 2>&1 || die "Docker が動いていません。OrbStack を起動してください"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  info ".env を .env.example から作りました"
fi
load_env

# 外付け SSD（LAB_DATA_DIR の親）がつながっているか
data_parent="$(dirname "$LAB_DATA_DIR")"
[[ -d "$data_parent" ]] || die "LAB_DATA_DIR の親フォルダ ${data_parent} がありません（外付け SSD がつながっているか確かめてください）"
mkdir -p "$LAB_DATA_DIR/chatgpt"

if lsof -nP -iTCP:"$LAB_HTTP_PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! cluster_exists; then
  die "ポート ${LAB_HTTP_PORT} が別のプロセスに使われています（.env の LAB_HTTP_PORT を変えてください）"
fi

# 手作業の準備の状況（無くても構築は進む。該当する機能だけ無効になる）
has_google  && ok "Google の OAuth クライアント: 設定あり" || warn "Google の OAuth クライアントが未設定です。SSO と Gmail の MCP は無効のまま構築します（docs/setup-google.md）"
has_users   && ok "検証ユーザー: 設定あり"                || warn "LAB_USER1_EMAIL が未設定です。利用者のキーは作りません"
has_jev     && ok "Jev の API キー: 設定あり"             || warn "TYPESAFE_API_KEY が未設定です。一次判定（Jev）を使わずに構築します"
has_chatgpt && ok "ChatGPT のトークン: あり"              || warn "ChatGPT のトークンがありません。make chatgpt-login で取得するまで、Codex と二次判定は動きません"
ok "前提の確認が終わりました"
