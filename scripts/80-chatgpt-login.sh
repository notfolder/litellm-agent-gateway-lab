#!/usr/bin/env bash
# chatgpt/ プロバイダのデバイスコードでのログイン（手作業。初回だけ）。
# トークン（auth.json）は LAB_DATA_DIR/chatgpt に保存され、クラスタを作り直しても残る。
# ログインの後に make up を流すと、Codex 用のモデルと二次判定が ChatGPT に切り替わる。
source "$(dirname "$0")/lib.sh"
load_env
step "80 ChatGPT のログイン"

if has_chatgpt; then
  ok "ChatGPT のトークンはあります（${LAB_DATA_DIR}/chatgpt/auth.json）"
  exit 0
fi
[[ -t 0 ]] || die "対話の操作が要ります。ターミナルで make chatgpt-login を実行してください"
[[ -f "$BUILD_DIR/litellm-image" ]] || "$ROOT/scripts/20-images.sh"
mkdir -p "$LAB_DATA_DIR/chatgpt"

image="litellm-lab:$(sed -n 2p "$BUILD_DIR/litellm-image")"
cat >&2 <<'EOF'

ブラウザで表示された URL を開き、表示されたコードを入力して ChatGPT にサインインしてください。
（ChatGPT の設定の「セキュリティ」で、デバイスコードでのログインを有効にしておく必要があります）
コードは他人に見せないでください。

EOF
docker run --rm -it \
  -v "$LAB_DATA_DIR/chatgpt:/token" -e CHATGPT_TOKEN_DIR=/token \
  --entrypoint python "$image" \
  -c 'from litellm.llms.chatgpt.authenticator import Authenticator; Authenticator().get_access_token(); print("OK")'

has_chatgpt || die "トークンが保存されませんでした"
chmod 600 "$LAB_DATA_DIR/chatgpt/auth.json"
ok "ログインしました。make up を流すと、Codex 用のモデルと二次判定が有効になります"
