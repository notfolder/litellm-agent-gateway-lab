#!/usr/bin/env bash
# .env の空の値を生成し、k8s の Secret に反映する。既存の値は上書きしない。
source "$(dirname "$0")/lib.sh"
load_env
step "30 秘密情報"

env_fill LITELLM_MASTER_KEY "$(gen_key)"
env_fill LITELLM_SALT_KEY "$(gen_key)"
env_fill POSTGRES_PASSWORD "$(gen_secret 16)"
env_fill REDIS_PASSWORD "$(gen_secret 16)"
for k in LAB_KEY_USER1_CLAUDE LAB_KEY_USER1_CODEX LAB_KEY_USER2_CLAUDE LAB_KEY_USER2_CODEX; do
  env_fill "$k" "$(gen_key)"
done
load_env

# Secret を宣言的に反映する（同じ内容なら unchanged）
apply_secret() {
  local name="$1"; shift
  kn create secret generic "$name" "$@" --dry-run=client -o yaml | kapply kn -f -
}

apply_secret postgres \
  --from-literal=username=litellm \
  --from-literal=password="$POSTGRES_PASSWORD"

apply_secret redis \
  --from-literal=password="$REDIS_PASSWORD"

litellm_args=(
  --from-literal=LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY"
  --from-literal=LITELLM_SALT_KEY="$LITELLM_SALT_KEY"
  --from-literal=REDIS_HOST=redis
  --from-literal=REDIS_PORT=6379
  --from-literal=REDIS_PASSWORD="$REDIS_PASSWORD"
)
if has_google; then
  # 管理画面の Google SSO（GOOGLE_*）と Gmail の MCP（GOOGLE_OAUTH_*）で同じクライアントを使う
  litellm_args+=(
    --from-literal=GOOGLE_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID"
    --from-literal=GOOGLE_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET"
    --from-literal=GOOGLE_OAUTH_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID"
    --from-literal=GOOGLE_OAUTH_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET"
  )
fi
if has_jev; then
  litellm_args+=(--from-literal=TYPESAFE_API_KEY="$TYPESAFE_API_KEY")
fi
apply_secret litellm-secrets "${litellm_args[@]}"

if has_google; then
  apply_secret workspace-mcp \
    --from-literal=GOOGLE_OAUTH_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID" \
    --from-literal=GOOGLE_OAUTH_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET"
fi
ok "Secret を反映しました"
