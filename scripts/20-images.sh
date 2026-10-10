#!/usr/bin/env bash
# 検証用 LiteLLM イメージの準備。
# - 中身（Dockerfile、ガードレール、ベースイメージ）のハッシュをタグにしてビルドする（同じタグがあればビルドしない）
# - 自作イメージの置き場に push する（置き場に同じタグがあれば push しない）
# 外部のイメージ（PostgreSQL など）は、ノードが Docker Hub の中継キャッシュ経由で取るので、ここでは扱わない
source "$(dirname "$0")/lib.sh"
load_env
step "20 イメージ"

litellm_tag="$( (echo "$LITELLM_BASE_IMAGE"; cd "$ROOT/litellm" && find Dockerfile guardrails -type f \
  ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 | sort -z | xargs -0 shasum -a 256) | sha_of)"
local_image="litellm-lab:$litellm_tag"
push_image="localhost:${LAB_REGISTRY_HOST_PORT}/litellm-lab:$litellm_tag"
# クラスタの中から見たイメージ名（Helm に渡す）
printf '%s\n' "${LAB_REGISTRY_IN_CLUSTER}/litellm-lab" "$litellm_tag" >"$BUILD_DIR/litellm-image"

if docker image inspect "$local_image" >/dev/null 2>&1; then
  ok "LiteLLM イメージ ${local_image} はビルド済みです"
else
  info "LiteLLM イメージ ${local_image} をビルドします（単体テストも流れます）"
  docker build --build-arg LITELLM_BASE_IMAGE="$LITELLM_BASE_IMAGE" -t "$local_image" "$ROOT/litellm"
fi

if curl -fsS -o /dev/null -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
  "http://localhost:${LAB_REGISTRY_HOST_PORT}/v2/litellm-lab/manifests/${litellm_tag}"; then
  ok "置き場に ${litellm_tag} があります"
else
  info "置き場に push します"
  docker tag "$local_image" "$push_image"
  # Mac 側には arm64 の中身しか無いので、arm64 だけを push する
  docker push --platform linux/arm64 -q "$push_image" >/dev/null
  ok "push しました: ${push_image}"
fi
