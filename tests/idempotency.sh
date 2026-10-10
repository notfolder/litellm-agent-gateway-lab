#!/usr/bin/env bash
# 冪等性の試験: make up を流した後にもう一度流し、2 回目で何も変わらないことを確かめる。
# 比べるもの: Helm のリビジョン、Pod の UID と再起動回数、ConfigMap / Secret の resourceVersion
source "$(dirname "$0")/../scripts/lib.sh"
load_env
step "冪等性の試験"

snapshot() {
  {
    helm --kube-context "$KUBE_CONTEXT" -n "$NS" list -o json | jq -r '.[] | "helm \(.name) rev=\(.revision)"'
    kn get pods -o json | jq -r '.items[] | select(.status.phase=="Running") | "pod \(.metadata.name) uid=\(.metadata.uid) restarts=\([.status.containerStatuses[].restartCount]|add)"'
    kn get configmap,secret -o json | jq -r '.items[] | select(.metadata.name|test("^sh\\.helm")|not) | "\(.kind) \(.metadata.name) rv=\(.metadata.resourceVersion)"'
    kn get deploy,statefulset -o json | jq -r '.items[] | "\(.kind) \(.metadata.name) gen=\(.metadata.generation)"'
  } | sort
}

info "1 回目の make up"
make -C "$ROOT" --no-print-directory up >/dev/null
before="$(snapshot)"
info "2 回目の make up"
make -C "$ROOT" --no-print-directory up >/dev/null
after="$(snapshot)"

if [[ "$before" == "$after" ]]; then
  ok "2 回目の make up で何も変わりませんでした（$(wc -l <<<"$before" | tr -d ' ') 項目を比較）"
else
  warn "2 回目の make up で変わったもの:"
  diff <(echo "$before") <(echo "$after") >&2 || true
  die "冪等ではありません"
fi
