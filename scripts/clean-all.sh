#!/usr/bin/env bash
# クラスタ、生成したクライアント設定、ビルドした LiteLLM イメージを消す。
# 残すもの: .env、ChatGPT のトークン（LAB_DATA_DIR）、イメージの置き場（Docker Hub のキャッシュを含む）
source "$(dirname "$0")/lib.sh"
step "全削除"
cat >&2 <<EOF
次を削除します:
  - クラスタ ${CLUSTER}（DB を含む）
  - clients/out/、.codex-lab/、.build/
  - ローカルの litellm-lab イメージ
残すもの: .env、ChatGPT のトークン、イメージの置き場
EOF
read -r -p "よろしいですか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { info "中止しました"; exit 0; }

"$ROOT/scripts/down.sh"
rm -rf "$ROOT/clients/out" "$ROOT/.codex-lab" "$BUILD_DIR"
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '(^|/)litellm-lab:' | xargs -r docker rmi >/dev/null || true
ok "削除しました"
