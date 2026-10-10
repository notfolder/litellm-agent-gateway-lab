#!/usr/bin/env bash
# ガードレールの単体テスト。LiteLLM のベースイメージ（Python 3.13 と litellm 入り）の中で流す
source "$(dirname "$0")/lib.sh"
step "単体テスト"
docker run --rm -v "$ROOT/litellm/guardrails:/g:ro" -w /g --entrypoint python "$LITELLM_BASE_IMAGE" \
  -m unittest discover -s tests -v
