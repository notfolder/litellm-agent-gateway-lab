#!/usr/bin/env bash
# LiteLLM（公式 Helm チャート）と入口（Traefik）。
# - プロキシ設定は .env から生成して ConfigMap にする。設定のハッシュを Pod の注釈に入れ、変わったときだけ再起動する
# - helm upgrade --install は、値が同じなら Pod を作り直さない。DB 移行は Helm フックの Job が毎回流す（移行自体は冪等）
source "$(dirname "$0")/lib.sh"
load_env
step "60 LiteLLM"

[[ -f "$BUILD_DIR/litellm-image" ]] || die "イメージ名がありません。先に 20-images を流してください"
{ read -r image_repo; read -r image_tag; } <"$BUILD_DIR/litellm-image"

# プロキシ設定の生成
LAB_HAS_CHATGPT=$(has_chatgpt && echo true || echo false)
export LAB_HAS_CHATGPT
config="$BUILD_DIR/litellm-config.yaml"
python3 "$ROOT/scripts/render-litellm-config.py" | write_if_changed "$config" && info "プロキシ設定を更新しました" || true
config_hash="$(sha_of <"$config")"
kn create configmap litellm-config --from-file=config.yaml="$config" --dry-run=client -o yaml | kapply kn -f -

# 値が同じかどうかを比べるため、渡す値をファイルにまとめる
overrides="$BUILD_DIR/litellm-overrides.yaml"
cat <<EOF | write_if_changed "$overrides" || true
image:
  repository: ${image_repo}
  tag: "${image_tag}"
podAnnotations:
  lab/config-hash: "${config_hash}"
envVars:
  LITELLM_MODE: PRODUCTION
  PROXY_BASE_URL: ${LAB_BASE_URL}
  CHATGPT_TOKEN_DIR: /var/lib/lab/chatgpt
  UI_USERNAME: admin
EOF

values_hash="$(cat "$ROOT/litellm/values.yaml" "$overrides" | sha_of)"

# 前回の helm が途中で止まり、リリースが pending のまま残っていれば片付ける（そのままだと次の helm が失敗する）
status="$(helm --kube-context "$KUBE_CONTEXT" -n "$NS" status "$HELM_RELEASE" -o json 2>/dev/null | jq -r '.info.status // empty' || true)"
case "$status" in
  pending-install)
    warn "途中で止まったインストール（${status}）を削除してから入れ直します"
    helm --kube-context "$KUBE_CONTEXT" -n "$NS" uninstall "$HELM_RELEASE" --wait ;;
  pending-upgrade|pending-rollback)
    warn "途中で止まった更新（${status}）を直前の版に戻してから入れ直します"
    helm --kube-context "$KUBE_CONTEXT" -n "$NS" rollback "$HELM_RELEASE" --wait --timeout 10m \
      || { warn "戻せなかったので削除して入れ直します"; helm --kube-context "$KUBE_CONTEXT" -n "$NS" uninstall "$HELM_RELEASE" --wait; } ;;
  failed)
    warn "前回の helm が失敗しています。入れ直します" ;;
esac
current_hash="$(helm --kube-context "$KUBE_CONTEXT" -n "$NS" get values "$HELM_RELEASE" -o json 2>/dev/null \
  | jq -r '.labValuesHash // empty' || true)"

if [[ "$current_hash" == "$values_hash" ]] && helm --kube-context "$KUBE_CONTEXT" -n "$NS" status "$HELM_RELEASE" -o json 2>/dev/null \
  | jq -e '.info.status == "deployed"' >/dev/null; then
  ok "LiteLLM のリリースは最新です（値のハッシュ ${values_hash}）"
else
  info "LiteLLM を helm upgrade --install します（DB 移行の Job が先に流れます）"
  helm --kube-context "$KUBE_CONTEXT" upgrade --install "$HELM_RELEASE" "$LITELLM_CHART" \
    --version "$LITELLM_CHART_VERSION" -n "$NS" \
    -f "$ROOT/litellm/values.yaml" -f "$overrides" \
    --set-string labValuesHash="$values_hash" \
    --wait --timeout 15m
fi

wait_until 600 "LiteLLM の起動" kn rollout status deploy/litellm --timeout=5s

# 入口（Traefik）
kapply kn -f "$ROOT/k8s/ingress.yaml"
wait_until 120 "入口からの応答（${LAB_BASE_URL}/health/liveliness）" http_ok "${LAB_BASE_URL}/health/liveliness"
ok "LiteLLM の準備ができました: ${LAB_BASE_URL}（管理画面 ${LAB_BASE_URL}/ui）"
