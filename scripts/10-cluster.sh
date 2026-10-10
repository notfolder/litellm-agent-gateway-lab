#!/usr/bin/env bash
# k3d クラスタの作成と Traefik の設定。クラスタが既にあれば作らない。
source "$(dirname "$0")/lib.sh"
load_env
step "10 クラスタ"

# イメージの置き場（クラスタより先に作る。クラスタを消しても残す）
ensure_registry "$LAB_REGISTRY_NAME" "$LAB_REGISTRY_HOST_PORT"
ensure_registry "$LAB_CACHE_NAME" "$LAB_CACHE_HOST_PORT" --proxy-remote-url https://registry-1.docker.io

cfg="$BUILD_DIR/k3d-config.yaml"
export K3S_IMAGE
render "$ROOT/k8s/k3d-config.yaml" '${K3S_IMAGE} ${LAB_HTTP_PORT} ${LAB_DATA_DIR} ${LAB_REGISTRY_NAME} ${LAB_CACHE_NAME}' \
  | write_if_changed "$cfg" || true

if cluster_exists; then
  # 既存のクラスタが設定と食い違っていないか（ノード数と k3s の版）
  nodes="$(k3d node list -o json | jq --arg c "$CLUSTER" '[.[] | select(.runtimeLabels["k3d.cluster"] == $c and (.role == "server" or .role == "agent"))] | length')"
  image="$(docker inspect "k3d-$CLUSTER-server-0" --format '{{.Config.Image}}' 2>/dev/null || true)"
  if [[ "$nodes" != "3" || "$image" != "$K3S_IMAGE" ]]; then
    die "既存のクラスタ（ノード ${nodes}、${image}）が設定（ノード 3、${K3S_IMAGE}）と違います。make rebuild で作り直してください"
  fi
  if ! docker exec "k3d-$CLUSTER-server-0" grep -q "$LAB_CACHE_NAME" /etc/rancher/k3s/registries.yaml 2>/dev/null; then
    die "既存のクラスタがイメージ置き場を使う設定になっていません。make rebuild で作り直してください"
  fi
  # 止まっていれば起動する
  if [[ "$(docker inspect -f '{{.State.Running}}' "k3d-$CLUSTER-server-0")" != "true" ]]; then
    info "停止中のクラスタを起動します"
    k3d cluster start "$CLUSTER"
  fi
  ok "クラスタ ${CLUSTER} は作成済みです"
else
  info "クラスタ ${CLUSTER} を作ります"
  k3d cluster create --config "$cfg"
fi

wait_until 180 "全ノードの Ready" kc wait --for=condition=Ready nodes --all --timeout=5s

kapply kc -f "$ROOT/k8s/traefik-config.yaml"
# Traefik が（再）展開されるのを待つ
wait_until 300 "Traefik の起動" kc -n kube-system rollout status deploy/traefik --timeout=5s
if ! kc -n kube-system get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q 'readTimeout=600s'; then
  warn "Traefik の起動引数にタイムアウトの設定が見えません（反映待ちの可能性があります）"
fi

kapply kc -f "$ROOT/k8s/namespace.yaml"
ok "クラスタの準備ができました"
