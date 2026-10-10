#!/usr/bin/env bash
# 構築スクリプトの共通関数。各スクリプトの先頭で source する。
# 方針（docs/design.md 14 章）: 状態を見てから動く。何度流しても同じ結果になる。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/.build"
ENV_FILE="$ROOT/.env"

CLUSTER=litellm
KUBE_CONTEXT="k3d-$CLUSTER"
NS=litellm-lab
HELM_RELEASE=litellm
LITELLM_CHART=oci://ghcr.io/berriai/litellm-helm
# LiteLLM の版（.env の LAB_LITELLM_VERSION で上書きできる。make upgrade が書き換える）
set_litellm_version() {
  LAB_LITELLM_VERSION="${LAB_LITELLM_VERSION:-v1.104.2}"
  LITELLM_BASE_IMAGE="ghcr.io/berriai/litellm-database:${LAB_LITELLM_VERSION}"
  LITELLM_CHART_VERSION="${LAB_LITELLM_VERSION#v}"
}
set_litellm_version
K3S_IMAGE=rancher/k3s:v1.35.5-k3s1
# イメージの置き場（クラスタとは別に作り、make down では消さない）
LAB_REGISTRY_NAME=litellm-registry.localhost   # 自作イメージを push する
LAB_REGISTRY_HOST_PORT=5050
LAB_CACHE_NAME=dockerio-cache.localhost        # Docker Hub の中継キャッシュ
LAB_CACHE_HOST_PORT=5051
# クラスタの中から見た自作イメージの置き場
LAB_REGISTRY_IN_CLUSTER="k3d-${LAB_REGISTRY_NAME}:5000"
export LAB_REGISTRY_NAME LAB_REGISTRY_HOST_PORT LAB_CACHE_NAME LAB_CACHE_HOST_PORT LAB_REGISTRY_IN_CLUSTER

mkdir -p "$BUILD_DIR"

# ---- ログ ----
_ts() { date +%H:%M:%S; }
info() { printf '\033[36m[%s] %s\033[0m\n' "$(_ts)" "$*" >&2; }
ok()   { printf '\033[32m[%s] %s\033[0m\n' "$(_ts)" "$*" >&2; }
warn() { printf '\033[33m[%s] 警告: %s\033[0m\n' "$(_ts)" "$*" >&2; }
die()  { printf '\033[31m[%s] エラー: %s\033[0m\n' "$(_ts)" "$*" >&2; exit 1; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*" >&2; }

# ---- .env ----
load_env() {
  [[ -f "$ENV_FILE" ]] || die ".env がありません。make up（00-preflight）で作られます"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  LAB_DATA_DIR="${LAB_DATA_DIR:-/Volumes/SSD/litellm-lab}"
  LAB_HTTP_PORT="${LAB_HTTP_PORT:-8080}"
  LAB_BASE_URL="http://localhost:${LAB_HTTP_PORT}"
  export LAB_DATA_DIR LAB_HTTP_PORT LAB_BASE_URL
  set_litellm_version
}

# .env の KEY を VALUE に書き換える（明示的な更新のときだけ使う。秘密情報には使わない）
env_set() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i '' -e "s|^${key}=.*$|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
  export "$key=$value"
}

# .env の KEY が空のときだけ VALUE を書き込む（既存の値は決して上書きしない）
env_fill() {
  local key="$1" value="$2" current
  current="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
  if [[ -n "$current" ]]; then
    return 0
  fi
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # 空の行を置き換える（macOS の sed）
    sed -i '' -e "s|^${key}=.*$|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
  export "$key=$value"
  info ".env に ${key} を生成しました"
}

gen_secret() { openssl rand -hex "${1:-24}"; }
gen_key() { printf 'sk-%s' "$(openssl rand -hex 24)"; }

has_google()  { [[ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" && -n "${GOOGLE_OAUTH_CLIENT_SECRET:-}" ]]; }
has_jev()     { [[ -n "${TYPESAFE_API_KEY:-}" ]]; }
# トークン（refresh_token か access_token）が入っているか。ログインの途中で止まると、
# 要求時刻だけの auth.json が残ることがあるので、ファイルの有無では判定しない
has_chatgpt() {
  jq -e '((.refresh_token // .access_token // "") | length) > 0' "$LAB_DATA_DIR/chatgpt/auth.json" >/dev/null 2>&1
}
has_users()   { [[ -n "${LAB_USER1_EMAIL:-}" ]]; }

# ---- ファイル ----
# 内容が変わったときだけ書き換える（stdin を DEST に）。変わったら 0、同じなら 1 を返す
write_if_changed() {
  local dest="$1" tmp
  tmp="$(mktemp "${dest}.XXXXXX")"
  cat >"$tmp"
  if [[ -f "$dest" ]] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$dest"
  return 0
}

# envsubst で、指定した変数だけを置き換える
render() {
  local src="$1" vars="$2"
  envsubst "$vars" <"$src"
}

sha_of() { shasum -a 256 | cut -c1-12; }

# ---- k8s ----
kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }
kn() { kubectl --context "$KUBE_CONTEXT" -n "$NS" "$@"; }

# 差分があるときだけ apply する（kubectl apply は変化がなくても configured と出ることがあるため）。
# kapply <kn|kc> -f <file|-> ...。stdin を使う場合は一時ファイルに落としてから比べる
kapply() {
  local runner="$1"; shift
  local tmp="" args=()
  while (($#)); do
    if [[ "$1" == "-f" && "${2:-}" == "-" ]]; then
      tmp="$(mktemp)"; cat >"$tmp"; args+=(-f "$tmp"); shift 2
    else
      args+=("$1"); shift
    fi
  done
  local rc=0
  "$runner" diff "${args[@]}" >/dev/null 2>&1 || rc=$?
  if [[ $rc -eq 0 ]]; then
    "$runner" get "${args[@]}" -o name 2>/dev/null | sed 's/$/ unchanged/' >&2
  elif [[ $rc -eq 1 ]]; then
    "$runner" apply "${args[@]}" >&2
  else
    # diff 自体が失敗した（CRD が未登録など）。そのまま apply する
    "$runner" apply "${args[@]}" >&2
  fi
  [[ -n "$tmp" ]] && rm -f "$tmp"
  return 0
}

registry_exists() {
  k3d registry list -o json 2>/dev/null | jq -e --arg n "k3d-$1" 'any(.[]; .name == $n)' >/dev/null
}

# 置き場が無ければ作り、止まっていれば起動する。ensure_registry <名前> <Mac 側のポート> [k3d の追加引数...]
ensure_registry() {
  local name="$1" port="$2"; shift 2
  if registry_exists "$name"; then
    if [[ "$(docker inspect -f '{{.State.Running}}' "k3d-$name")" != "true" ]]; then
      docker start "k3d-$name" >/dev/null
    fi
    ok "イメージ置き場 k3d-${name} は作成済みです"
  else
    info "イメージ置き場 k3d-${name} を作ります"
    k3d registry create "$name" --port "127.0.0.1:${port}" "$@"
  fi
  wait_until 60 "k3d-${name} の応答" http_ok "http://localhost:${port}/v2/"
}

cluster_exists() {
  k3d cluster list -o json 2>/dev/null | jq -e --arg n "$CLUSTER" 'any(.[]; .name == $n)' >/dev/null
}

# 条件（コマンド）が成功するまで待つ。wait_until <秒> <説明> <コマンド...>
wait_until() {
  local timeout="$1" desc="$2"; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS - start >= timeout )); then
      die "${desc} を ${timeout} 秒待ちましたが、終わりませんでした"
    fi
    sleep 3
  done
  ok "${desc}（$(( SECONDS - start )) 秒）"
}

http_ok() { curl -fsS -o /dev/null --max-time 5 "$1"; }

# LiteLLM の管理 API を呼ぶ。admin_api <METHOD> <PATH> [JSON]
admin_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS --max-time 30 -X "$method" "$LAB_BASE_URL$path"
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'content-type: application/json')
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}
