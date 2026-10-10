#!/usr/bin/env bash
# 負荷試験（k6 を Docker で動かす）。相手は偽 LLM なので、利用枠を使わない。
# 環境変数: VUS（同時接続数、既定 20）、DURATION（既定 2m）、SCENARIO（chat | review、既定 chat）
source "$(dirname "$0")/../../scripts/lib.sh"
load_env
step "負荷試験"
docker run --rm -i --add-host=host.docker.internal:host-gateway \
  -e BASE_URL="http://host.docker.internal:${LAB_HTTP_PORT}" -e KEY="$LITELLM_MASTER_KEY" \
  -e VUS="${VUS:-20}" -e DURATION="${DURATION:-2m}" -e SCENARIO="${SCENARIO:-chat}" \
  grafana/k6:1.3.0 run - <"$(dirname "$0")/k6.js"
info "試験中の Pod 数の変化は kubectl --context $KUBE_CONTEXT -n $NS get hpa,pods -w で見られます"
