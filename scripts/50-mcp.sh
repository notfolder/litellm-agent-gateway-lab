#!/usr/bin/env bash
# MCP サーバー（google_workspace_mcp）と、審査の試験用の偽 LLM（fake-llm）。
# google_workspace_mcp は Google の OAuth クライアントが設定されているときだけ置く。
source "$(dirname "$0")/lib.sh"
load_env
step "50 MCP サーバーと試験用の偽 LLM"

# 偽 LLM: サーバーのスクリプトを ConfigMap にし、中身のハッシュを注釈に入れる（変わったときだけ再起動される）
kn create configmap fake-llm-server --from-file=server.py="$ROOT/tests/fake-llm/server.py" \
  --dry-run=client -o yaml | kapply kn -f -
FAKE_LLM_HASH="$(sha_of <"$ROOT/tests/fake-llm/server.py")"
export FAKE_LLM_HASH
render "$ROOT/k8s/fake-llm.yaml" '${FAKE_LLM_HASH}' | kapply kn -f -
wait_until 180 "偽 LLM の起動" kn rollout status deploy/fake-llm --timeout=5s

if has_google; then
  kapply kn -f "$ROOT/k8s/workspace-mcp.yaml"
  wait_until 300 "google_workspace_mcp の起動" kn rollout status deploy/workspace-mcp --timeout=5s
else
  # 設定が消えた場合に備えて、置いてあれば片付ける
  if kn get deploy workspace-mcp >/dev/null 2>&1; then
    kn delete -f "$ROOT/k8s/workspace-mcp.yaml" --ignore-not-found
  fi
  warn "Google の OAuth クライアントが未設定なので、google_workspace_mcp は置きません"
fi
ok "MCP サーバーの段階が終わりました"
