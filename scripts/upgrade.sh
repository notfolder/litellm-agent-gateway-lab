#!/usr/bin/env bash
# LiteLLM を別の版でローリング更新する（spec 6.3 の無停止更新の試験）。
# .env の LAB_LITELLM_VERSION を書き換えるので、以後の make up もこの版になる。
source "$(dirname "$0")/lib.sh"
load_env
version="${1:?版を指定してください（例: v1.104.3）}"
step "LiteLLM を ${LAB_LITELLM_VERSION} から ${version} に更新"
docker manifest inspect "ghcr.io/berriai/litellm-database:${version}" >/dev/null 2>&1 \
  || die "イメージ ghcr.io/berriai/litellm-database:${version} が見つかりません"
env_set LAB_LITELLM_VERSION "$version"
"$ROOT/scripts/20-images.sh"
"$ROOT/scripts/60-litellm.sh"
ok "${version} に更新しました"
