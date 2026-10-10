#!/usr/bin/env bash
# 状態の表示（何も変更しない）
source "$(dirname "$0")/lib.sh"
load_env
step "状態"

if ! cluster_exists; then
  warn "クラスタ ${CLUSTER} はありません（make up で作ります）"
  exit 0
fi
kn get pods -o wide 2>/dev/null || true
echo >&2
helm --kube-context "$KUBE_CONTEXT" -n "$NS" list 2>/dev/null || true
echo >&2

yn() { "$@" && echo "あり" || echo "なし"; }
cat >&2 <<EOF
入口:            ${LAB_BASE_URL}（管理画面 ${LAB_BASE_URL}/ui）
Google OAuth:    $(yn has_google)   → SSO と Gmail の MCP
Jev の API キー: $(yn has_jev)      → 一次判定（LAB_USE_JEV=${LAB_USE_JEV:-true}）
ChatGPT:         $(yn has_chatgpt)  → Codex 用のモデルと二次判定（なしなら二次判定は偽 LLM）
検証ユーザー:    ${LAB_USER1_EMAIL:-（未設定）} ${LAB_USER2_EMAIL:-}
クライアント:    clients/claude-lab、clients/codex-lab
EOF
if http_ok "${LAB_BASE_URL}/health/liveliness"; then
  ok "LiteLLM は応答しています"
else
  warn "LiteLLM が応答しません"
fi
