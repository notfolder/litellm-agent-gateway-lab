#!/usr/bin/env bash
# PostgreSQL と Redis。kubectl apply で差分だけ反映し、Ready を待つ。
source "$(dirname "$0")/lib.sh"
load_env
step "40 PostgreSQL と Redis"

kapply kn -f "$ROOT/k8s/postgres.yaml" -f "$ROOT/k8s/redis.yaml"
wait_until 300 "PostgreSQL の起動" kn rollout status statefulset/postgres --timeout=5s
wait_until 180 "Redis の起動" kn rollout status deploy/redis --timeout=5s
ok "PostgreSQL と Redis の準備ができました"
