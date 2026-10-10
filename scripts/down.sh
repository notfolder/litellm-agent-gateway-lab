#!/usr/bin/env bash
# クラスタを削除する。DB（クラスタ内）は消える。
# 残すもの: .env、chatgpt のトークン（LAB_DATA_DIR）、イメージの置き場（自作イメージと Docker Hub のキャッシュ）
source "$(dirname "$0")/lib.sh"
step "クラスタの削除"
if cluster_exists; then
  k3d cluster delete "$CLUSTER"
  ok "クラスタ ${CLUSTER} を削除しました"
else
  ok "クラスタ ${CLUSTER} はありません"
fi
