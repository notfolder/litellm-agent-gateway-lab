#!/usr/bin/env bash
# ECS 構成（LAB_TARGET=aws-ecs）の構築スクリプト。1 ファイルで持ち運べるようにしてある。
# 変更仕様書・変更設計書: .history/2026-11-21-ECS構成/
#
# 使い方: ecs/ecs-lab.sh <up|down|status|smoke|forward|render-config|render-template>
# 使う道具: bash（3.2 以上）、aws CLI v2、docker（buildx）、curl、openssl。forward だけ Session Manager のプラグイン。
# 設定は .env（ECS_LAB_ENV で別のファイルを指せる）。何度流しても同じ結果になる。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ECS_LAB_ENV:-$ROOT/.env}"
BUILD="${ECS_LAB_BUILD:-$ROOT/.build/ecs}"
mkdir -p "$BUILD"

say()  { printf '\033[36m==>\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32m ok\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERR\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- .env
[ -f "$ENV_FILE" ] || die ".env がありません（${ENV_FILE}）"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in \#*|'') continue ;; esac
  if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
    printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
  fi
done < "$ENV_FILE"

env_set() {  # KEY VALUE: .env の KEY を書き換える（無ければ足す）
  local key="$1" value="$2" tmp="$ENV_FILE.tmp.$$"
  if grep -q "^$key=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k && !done {print k "=" v; done=1; next} {print}' "$ENV_FILE" > "$tmp"
  else
    cp "$ENV_FILE" "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  printf -v "$key" '%s' "$value"
}
env_fill() {  # KEY VALUE: 空のときだけ書く
  local cur; eval "cur=\${$1:-}"
  if [ -z "$cur" ]; then env_set "$1" "$2"; ok ".env に $1 を生成しました"; fi
}
gen() { openssl rand -hex "${1:-24}"; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
is_true() { [ "$(lower "${1:-}")" = "true" ]; }

PREFIX="${LAB_ECS_PREFIX:-litellm-ecs}"
REGION="${LAB_AWS_REGION:-ap-northeast-1}"
SCALE="${LAB_ECS_SCALE:-lab}"
INFRA="${LAB_ECS_INFRA:-managed}"
STACK="$PREFIX-ecs"
HOSTNAME_="${LAB_ECS_HOSTNAME:-}"
LOCAL_PORT="${LAB_ECS_LOCAL_PORT:-18443}"
ZABBIX_LOCAL_PORT="${LAB_ECS_ZABBIX_LOCAL_PORT:-18080}"
NS="$PREFIX.internal"
case "$SCALE" in lab|prod) ;; *) die "LAB_ECS_SCALE は lab か prod です（${SCALE}）" ;; esac
case "$INFRA" in managed|provided) ;; *) die "LAB_ECS_INFRA は managed か provided です（${INFRA}）" ;; esac

awsc() {
  if [ -n "${LAB_AWS_PROFILE:-}" ]; then aws --profile "$LAB_AWS_PROFILE" --region "$REGION" "$@"
  else aws --region "$REGION" "$@"; fi
}
local_hostname() {  # 端末の別名（A レコード 127.0.0.1、証明書の SAN）。既定はホスト名の最初の名前に -local
  if [ -n "${LAB_ECS_LOCAL_HOSTNAME:-}" ]; then printf '%s' "$LAB_ECS_LOCAL_HOSTNAME"; return; fi
  printf '%s-local.%s' "${HOSTNAME_%%.*}" "${HOSTNAME_#*.}"
}

# ---------------------------------------------------------------- 端末の中継（社内プロキシ）
apply_operator_proxy() {  # URL: このプロセスと子プロセス（aws、docker の取得、curl）を中継に通す
  local proxy="$1" ca="$2"
  [ -n "$proxy" ] || return 0
  [ -s "$ca" ] || die "中継の CA がありません（${ca}）"
  local sys_bundle=/etc/ssl/cert.pem
  [ -f "$sys_bundle" ] || sys_bundle=/etc/ssl/certs/ca-certificates.crt
  cat "$sys_bundle" "$ca" > "$BUILD/operator-ca-bundle.pem"
  export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy"
  export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
  export AWS_CA_BUNDLE="$BUILD/operator-ca-bundle.pem" CURL_CA_BUNDLE="$BUILD/operator-ca-bundle.pem" \
         SSL_CERT_FILE="$BUILD/operator-ca-bundle.pem" REQUESTS_CA_BUNDLE="$BUILD/operator-ca-bundle.pem"
  ok "端末の通信を中継に通します: $proxy"
}

# ---------------------------------------------------------------- Cloudflare
CF_API=https://api.cloudflare.com/client/v4
cf() { curl -sS --fail-with-body -H "Authorization: Bearer ${LAB_CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json" "$@"; }
cf_zone() {  # ホスト名からゾーン ID（長い候補から）
  local name="$1" out id
  while [[ "$name" == *.* ]]; do
    out=$(cf "$CF_API/zones?name=$name")
    id=$(printf '%s' "$out" | sed -n 's/.*"result":\[{"id":"\([0-9a-f]*\)".*/\1/p')
    if [ -n "$id" ]; then printf '%s' "$id"; return; fi
    name="${name#*.}"
  done
  die "Cloudflare のゾーンが見つかりません（$1）"
}
cf_record_id() {  # ZONE TYPE NAME
  cf "$CF_API/zones/$1/dns_records?type=$2&name=$3" | sed -n 's/.*"result":\[{"id":"\([0-9a-f]*\)".*/\1/p'
}
cf_upsert() {  # TYPE NAME CONTENT
  local zone id body
  zone=$(cf_zone "$2"); id=$(cf_record_id "$zone" "$1" "$2")
  body=$(printf '{"type":"%s","name":"%s","content":"%s","ttl":60,"proxied":false}' "$1" "$2" "$3")
  if [ -n "$id" ]; then
    if cf "$CF_API/zones/$zone/dns_records/$id" | grep -q "\"content\":\"$3\""; then return 0; fi
    cf -X PUT "$CF_API/zones/$zone/dns_records/$id" --data "$body" >/dev/null
  else
    cf -X POST "$CF_API/zones/$zone/dns_records" --data "$body" >/dev/null
  fi
  ok "DNS: $2 $1 → $3"
}
cf_delete() {  # TYPE NAME
  local zone id
  zone=$(cf_zone "$2"); id=$(cf_record_id "$zone" "$1" "$2")
  [ -z "$id" ] || { cf -X DELETE "$CF_API/zones/$zone/dns_records/$id" >/dev/null; ok "DNS: $2 を消しました"; }
}

# ---------------------------------------------------------------- 名前
account() { [ -n "${ACCOUNT:-}" ] || ACCOUNT=$(awsc sts get-caller-identity --query Account --output text); printf '%s' "$ACCOUNT"; }
bucket() { printf '%s-%s-%s' "$PREFIX" "$(account)" "$1"; }   # 論理名 → バケット名（EKS 構成と同じ形）
registry() { printf '%s.dkr.ecr.%s.amazonaws.com' "$(account)" "$REGION"; }
stack_out() {  # 出力の名前 → 値（スタックが無ければ空）
  awsc cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null | sed 's/^None$//'
}
stack_param() {
  awsc cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue" --output text 2>/dev/null | sed 's/^None$//'
}
stack_exists() { awsc cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; }
hash_of() { shasum -a 256 | cut -c1-12; }
dir_hash() {  # ビルドの材料の中身のハッシュ（__pycache__ と .pyc を除く）
  (cd "$1" && find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | LC_ALL=C sort | while IFS= read -r f; do
     printf '%s\0' "$f"; shasum -a 256 < "$f"; done) | hash_of
}

# ---------------------------------------------------------------- 中継の CA
egress_ca_file() {  # タスクと端末が信頼する中継の CA（managed は検証の中継の CA、provided は LAB_EGRESS_CA_FILE）
  if [ "$INFRA" = managed ]; then printf '%s' "$BUILD/relay-ca.pem"; else printf '%s' "${LAB_EGRESS_CA_FILE:-}"; fi
}
ensure_relay_ca() {  # 検証の中継（mitmproxy）の CA を端末の openssl で作る（作り直しても同じ CA を使い続ける）
  [ "$INFRA" = managed ] || return 0
  if [ ! -s "$BUILD/relay-ca.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 -subj "/CN=$PREFIX relay CA/O=litellm-lab" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" -keyout "$BUILD/relay-ca.key" -out "$BUILD/relay-ca.pem" 2>/dev/null
    chmod 600 "$BUILD/relay-ca.key"
    ok "検証の中継の CA を作りました（$BUILD/relay-ca.pem）"
  fi
}

# ---------------------------------------------------------------- 前提の確認
preflight() {
  say "道具と .env の確認"
  local t
  for t in aws docker curl openssl shasum; do command -v "$t" >/dev/null || die "$t がありません"; done
  docker buildx version >/dev/null 2>&1 || die "docker buildx がありません"
  [ -n "$HOSTNAME_" ] || die "LAB_ECS_HOSTNAME（入口のホスト名）が要ります"
  [ -n "${LAB_CLOUDFLARE_API_TOKEN:-}" ] || die "LAB_CLOUDFLARE_API_TOKEN（DNS のレコードの登録）が要ります"
  [ -n "${LAB_JUDGE_ANTHROPIC_API_KEY:-}" ] || warn "LAB_JUDGE_ANTHROPIC_API_KEY が無いので、審査官は呼べません"
  if [ "$INFRA" = provided ]; then
    [ -n "${LAB_ECS_VPC_ID:-}" ] && [ -n "${LAB_ECS_SUBNET_IDS:-}" ] || die "provided には LAB_ECS_VPC_ID と LAB_ECS_SUBNET_IDS が要ります"
    [ -n "${LAB_ECS_BASTION_CIDR:-}" ] || die "provided には LAB_ECS_BASTION_CIDR（踏み台の CIDR）が要ります"
    if [ -n "${LAB_EGRESS_PROXY:-}" ]; then [ -s "${LAB_EGRESS_CA_FILE:-}" ] || die "LAB_EGRESS_PROXY には LAB_EGRESS_CA_FILE が要ります"; fi
    check_provided_vpc
  fi
  env_fill LITELLM_MASTER_KEY "sk-$(gen 24)"
  env_fill LITELLM_SALT_KEY "sk-$(gen 24)"
  env_fill POSTGRES_PASSWORD "$(gen 20)"
  env_fill REDIS_PASSWORD "$(gen 20)"
  env_fill SENTINEL_PASSWORD "$(gen 20)"
  env_fill AUDIT_SESSION_SECRET "$(gen 32)"
  env_fill LAB_ZABBIX_ADMIN_PASSWORD "$(gen 12)"
  account >/dev/null
  # 初めて ECS を使うアカウントでは、サービスにリンクされたロールがまだ無く、クラスタの作成が失敗する（2026-11-21 に判明）
  local svc
  for svc in ecs.amazonaws.com ecs.application-autoscaling.amazonaws.com; do
    awsc iam create-service-linked-role --aws-service-name "$svc" >/dev/null 2>&1 && ok "サービスにリンクされたロールを作りました: $svc" || true
  done
  account >/dev/null
  ok "アカウント $(account)、リージョン ${REGION}、スタック ${STACK}（$SCALE / ${INFRA}）"
}
check_provided_vpc() {  # 用意された VPC に、要る VPC エンドポイントがあるか
  local have svc missing=""
  have=$(awsc ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$LAB_ECS_VPC_ID" \
           --query 'VpcEndpoints[].ServiceName' --output text | tr '\t' '\n')
  for svc in s3 ecr.api ecr.dkr logs ssm ssmmessages ec2messages monitoring ecs; do
    printf '%s\n' "$have" | grep -q "\.$svc\$" || missing="$missing $svc"
  done
  [ -z "$missing" ] || die "用意された VPC に VPC エンドポイントが足りません:${missing}（com.amazonaws.$REGION.<名前>）"
  [ "$(printf '%s' "$LAB_ECS_SUBNET_IDS" | tr ',' '\n' | grep -c .)" = 3 ] || die "LAB_ECS_SUBNET_IDS は 3 つ（3 AZ）です"
}

# ---------------------------------------------------------------- 設定のバケット
ensure_config_bucket() {
  local b; b=$(bucket config)
  if ! awsc s3api head-bucket --bucket "$b" >/dev/null 2>&1; then
    awsc s3api create-bucket --bucket "$b" --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    ok "設定のバケットを作りました: $b"
  fi
  awsc s3api put-public-access-block --bucket "$b" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  awsc s3api put-bucket-versioning --bucket "$b" --versioning-configuration Status=Enabled
  awsc s3api put-bucket-encryption --bucket "$b" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  awsc s3api put-bucket-policy --bucket "$b" --policy "$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"TlsOnly","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}' "$b" "$b")"
}
put_config() {  # 置き場のキー ファイル: 中身が同じなら置き直さない
  local key="$1" file="$2" b etag sum
  b=$(bucket config)
  sum=$(shasum -a 256 < "$file" | cut -c1-64)
  etag=$(awsc s3api head-object --bucket "$b" --key "$key" --query 'Metadata.sha256' --output text 2>/dev/null || true)
  [ "$etag" = "$sum" ] && return 0
  awsc s3api put-object --bucket "$b" --key "$key" --body "$file" --metadata "sha256=$sum" >/dev/null
}

# ---------------------------------------------------------------- 証明書
ensure_certificate() {
  say "TLS の証明書"
  local arn host local_host
  host="$HOSTNAME_"; local_host=$(local_hostname)
  if [ -n "${LAB_TLS_CERT_FILE:-}" ]; then
    # 社内で発行した証明書を ACM に取り込む（中身が変われば同じ ARN に取り込み直す）
    local sum prev args
    sum=$(cat "$LAB_TLS_CERT_FILE" "${LAB_TLS_CHAIN_FILE:-/dev/null}" | hash_of)
    arn=$(cat "$BUILD/cert-arn" 2>/dev/null || true); prev=$(cat "$BUILD/cert-sum" 2>/dev/null || true)
    if [ -z "$arn" ] || [ "$sum" != "$prev" ]; then
      args=(--certificate "fileb://$LAB_TLS_CERT_FILE" --private-key "fileb://$LAB_TLS_KEY_FILE")
      [ -z "${LAB_TLS_CHAIN_FILE:-}" ] || args+=(--certificate-chain "fileb://$LAB_TLS_CHAIN_FILE")
      [ -z "$arn" ] || args+=(--certificate-arn "$arn")
      arn=$(awsc acm import-certificate "${args[@]}" --query CertificateArn --output text)
      printf '%s' "$arn" > "$BUILD/cert-arn"; printf '%s' "$sum" > "$BUILD/cert-sum"
      ok "証明書を取り込みました"
    fi
  else
    arn=$(awsc acm list-certificates --certificate-statuses ISSUED PENDING_VALIDATION \
            --query "CertificateSummaryList[?DomainName=='$host'].CertificateArn | [0]" --output text | sed 's/^None$//')
    if [ -z "$arn" ]; then
      arn=$(awsc acm request-certificate --domain-name "$host" --subject-alternative-names "$host" "$local_host" \
              --validation-method DNS --idempotency-token "$(printf '%s' "$PREFIX" | tr -cd 'a-zA-Z0-9' | cut -c1-32)" \
              --query CertificateArn --output text)
      ok "証明書を申請しました"
    fi
    local status rec i
    status=$(awsc acm describe-certificate --certificate-arn "$arn" --query Certificate.Status --output text)
    if [ "$status" != ISSUED ]; then
      local want
      for i in $(seq 1 60); do   # 全部の名前の検証のレコードが出るまで待つ
        want=$(awsc acm describe-certificate --certificate-arn "$arn" --query 'length(Certificate.DomainValidationOptions)' --output text)
        rec=$(awsc acm describe-certificate --certificate-arn "$arn" \
                --query 'Certificate.DomainValidationOptions[?ResourceRecord].ResourceRecord.[Name,Value]' --output text)
        [ "$(printf '%s\n' "$rec" | grep -c .)" -ge "$want" ] && break
        sleep 2
      done
      printf '%s\n' "$rec" | sort -u | while read -r name value; do
        [ -n "$name" ] && cf_upsert CNAME "${name%.}" "${value%.}"
      done
      say "証明書の発行を待ちます（数分）"
      awsc acm wait certificate-validated --certificate-arn "$arn"
    fi
    printf '%s' "$arn" > "$BUILD/cert-arn"
  fi
  cf_upsert A "$local_host" 127.0.0.1
  CERT_ARN="$arn"
  ok "証明書: $arn"
}

# ---------------------------------------------------------------- イメージ
REPOS="litellm audit fake-llm mcp mirror/valkey mirror/pgbouncer mirror/workspace-mcp"
MIRROR_VALKEY=docker.io/valkey/valkey:9.0.6
MIRROR_PGBOUNCER=ghcr.io/cloudnative-pg/pgbouncer:1.25.1
MIRROR_MCP=ghcr.io/taylorwilsdon/google_workspace_mcp:2.1.0
LITELLM_BASE=ghcr.io/berriai/litellm-database:v1.104.2

ensure_repos() {
  local r
  for r in $REPOS; do
    if ! awsc ecr describe-repositories --repository-names "$PREFIX/$r" >/dev/null 2>&1; then
      awsc ecr create-repository --repository-name "$PREFIX/$r" --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256 >/dev/null
      awsc ecr put-lifecycle-policy --repository-name "$PREFIX/$r" --lifecycle-policy-text \
        '{"rules":[{"rulePriority":1,"description":"keep 10","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},"action":{"type":"expire"}}]}' >/dev/null
      ok "ECR: $PREFIX/$r"
    fi
  done
  awsc ecr get-login-password | docker login --username AWS --password-stdin "$(registry)" >/dev/null 2>&1 \
    || die "ECR にログインできません"
}
image_exists() { awsc ecr describe-images --repository-name "$1" --image-ids "imageTag=$2" >/dev/null 2>&1; }
mirror() {  # 外のイメージを ECR に写す（Fargate は社内プロキシ越しに外から取れないため）。全プラットフォームをそのまま写す
  local src="$1" repo="$PREFIX/$2" tag="${1##*:}" dst
  dst="$(registry)/$repo:$tag"
  if ! image_exists "$repo" "$tag"; then
    if [ -n "${HTTPS_PROXY:-}" ]; then
      # 中継を通すとき: macOS の Go の道具（buildx）は SSL_CERT_FILE を読まず、中継の CA を信頼できない（VM 構成の helm と同じ）。
      # CA を読める Linux のコンテナの中の skopeo で写す
      docker run --rm -e "HTTPS_PROXY=$HTTPS_PROXY" -e "HTTP_PROXY=$HTTPS_PROXY" -e SSL_CERT_FILE=/lab/ca.pem \
        -v "$BUILD/operator-ca-bundle.pem:/lab/ca.pem:ro" quay.io/skopeo/stable:v1.20 copy --all --retry-times 3 \
        --dest-creds "AWS:$(awsc ecr get-login-password)" "docker://$src" "docker://$dst" >&2 || die "写せませんでした: $src"
    else
      docker buildx imagetools create --tag "$dst" "$src" >/dev/null || die "写せませんでした: $src"
    fi
    image_exists "$repo" "$tag" || die "写した後のイメージが ECR にありません: $dst"
    ok "写しました: $src"
  fi
  printf '%s' "$dst"
}
build_images() {
  say "イメージ（写しとビルド）"
  ensure_repos
  local ca tag ctx
  ca=$(egress_ca_file)
  IMG_VALKEY=$(mirror "$MIRROR_VALKEY" mirror/valkey) || exit 1
  IMG_PGBOUNCER=$(mirror "$MIRROR_PGBOUNCER" mirror/pgbouncer) || exit 1
  local mcp_src; mcp_src=$(mirror "$MIRROR_MCP" mirror/workspace-mcp) || exit 1
  # 中継の CA（無ければ空）。自作イメージに入れる（Fargate は CA をマウントする手段が無い）
  mkdir -p "$BUILD/ca"
  if [ -n "$ca" ] && [ -s "$ca" ]; then cp "$ca" "$BUILD/ca/egress-ca.pem"; else : > "$BUILD/ca/egress-ca.pem"; fi
  cp "$BUILD/ca/egress-ca.pem" "$ROOT/apps/audit/egress-ca.pem"
  local proxy_args=()
  if [ -n "${HTTPS_PROXY:-}" ]; then proxy_args=(--build-arg "HTTP_PROXY=$HTTPS_PROXY" --build-arg "HTTPS_PROXY=$HTTPS_PROXY"); fi
  # LiteLLM: 今の Dockerfile に、中継の CA を足した CA の束（/etc/lab/ca-bundle.pem）を重ねる
  if [ -n "${LAB_IMAGE_LITELLM:-}" ]; then IMG_LITELLM="$LAB_IMAGE_LITELLM"; else
    tag=$( { printf '%s' "$LITELLM_BASE"; dir_hash "$ROOT/litellm/guardrails"; shasum -a 256 < "$ROOT/litellm/Dockerfile"; shasum -a 256 < "$BUILD/ca/egress-ca.pem"; } | hash_of)
    IMG_LITELLM="$(registry)/$PREFIX/litellm:$tag"
    if ! image_exists "$PREFIX/litellm" "$tag"; then
      { cat "$ROOT/litellm/Dockerfile"; printf '\nCOPY --from=labca egress-ca.pem /tmp/egress-ca.pem\nRUN mkdir -p /etc/lab && cat /etc/ssl/certs/ca-certificates.crt /tmp/egress-ca.pem > /etc/lab/ca-bundle.pem\n'; } > "$BUILD/litellm.Dockerfile"
      docker buildx build --platform linux/arm64 --provenance=false "${proxy_args[@]+"${proxy_args[@]}"}" \
        --build-arg "LITELLM_BASE_IMAGE=$LITELLM_BASE" --build-context "labca=$BUILD/ca" \
        -f "$BUILD/litellm.Dockerfile" -t "$IMG_LITELLM" --push "$ROOT/litellm" >&2
      ok "LiteLLM: $IMG_LITELLM"
    fi
  fi
  if [ -n "${LAB_IMAGE_AUDIT:-}" ]; then IMG_AUDIT="$LAB_IMAGE_AUDIT"; else
    tag=$(dir_hash "$ROOT/apps/audit")
    IMG_AUDIT="$(registry)/$PREFIX/audit:$tag"
    if ! image_exists "$PREFIX/audit" "$tag"; then
      docker buildx build --platform linux/arm64 --provenance=false "${proxy_args[@]+"${proxy_args[@]}"}" \
        -t "$IMG_AUDIT" --push "$ROOT/apps/audit" >&2
      ok "監査アプリ: $IMG_AUDIT"
    fi
  fi
  # google_workspace_mcp: 写したイメージに中継の CA を足した CA の束を重ねる（Google の API へは中継を通る）
  ctx="$BUILD/mcp"; mkdir -p "$ctx"
  cp "$BUILD/ca/egress-ca.pem" "$ctx/egress-ca.pem"
  printf 'FROM %s\nUSER root\nCOPY egress-ca.pem /tmp/egress-ca.pem\nRUN mkdir -p /etc/lab && cat /etc/ssl/certs/ca-certificates.crt /tmp/egress-ca.pem > /etc/lab/ca-bundle.pem\n' "$mcp_src" > "$ctx/Dockerfile"
  tag=$(dir_hash "$ctx")
  IMG_MCP="$(registry)/$PREFIX/mcp:$tag"
  if ! image_exists "$PREFIX/mcp" "$tag"; then
    docker buildx build --platform linux/arm64 --provenance=false "${proxy_args[@]+"${proxy_args[@]}"}" -t "$IMG_MCP" --push "$ctx" >&2
    ok "MCP: $IMG_MCP"
  fi
  ctx="$BUILD/fake-llm"; mkdir -p "$ctx"
  cp "$ROOT/tests/fake-llm/server.py" "$ctx/server.py"
  printf 'FROM python:3.13-alpine\nCOPY server.py /srv/server.py\nCMD ["python", "-u", "/srv/server.py"]\n' > "$ctx/Dockerfile"
  tag=$(dir_hash "$ctx")
  IMG_FAKE="$(registry)/$PREFIX/fake-llm:$tag"
  if ! image_exists "$PREFIX/fake-llm" "$tag"; then
    docker buildx build --platform linux/arm64 --provenance=false "${proxy_args[@]+"${proxy_args[@]}"}" \
      -t "$IMG_FAKE" --push "$ctx" >&2
    ok "偽 LLM: $IMG_FAKE"
  fi
}

# ---------------------------------------------------------------- LiteLLM の設定（labtool/render.py の litellm_config と同じ中身）
yaml_str() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }
indent() { sed "s/^/$1/"; }
policy_text() {  # 判定基準の文章（前後の空行と空白を落とす）
  local f="${LAB_ISOLATED_POLICY_FILE:-litellm/guardrails/policies/confidential.md}"
  case "$f" in /*) ;; *) f="$ROOT/$f" ;; esac
  [ -s "$f" ] || die "判定基準のファイルが空です: $f"
  awk 'NF {found=1} found' "$f" | awk '{lines[NR]=$0} END {n=NR; while (n>0 && lines[n] ~ /^[[:space:]]*$/) n--; for (i=1;i<=n;i++) print lines[i]}' \
    | sed -e '1s/^[[:space:]]*//' -e '$s/[[:space:]]*$//'
}
render_config() {
  local judge="${LAB_VM_JUDGE_MODEL:-claude-haiku-4-5-20251001}"
  local isolated content pool_limit dests d
  isolated=$(lower "${LAB_ISOLATED_REVIEW:-true}")
  content=false; [ "$isolated" = true ] && [ "$(lower "${LAB_ISOLATED_CONTENT:-true}")" = true ] && content=true
  pool_limit=10; [ "${LAB_DB_POOLER:-pooler}" = direct ] && pool_limit=5
  cat <<EOF
model_list:
- model_name: anthropic/*
  litellm_params:
    model: anthropic/*
- model_name: fake-tools
  litellm_params:
    model: openai/fake-tools
    api_key: sk-fake
    api_base: http://fake-llm:8000/v1
  model_info:
    input_cost_per_token: 1.0e-06
    output_cost_per_token: 2.0e-06
- model_name: review-judge
  litellm_params:
    model: anthropic/$judge
    api_key: os.environ/LAB_JUDGE_ANTHROPIC_API_KEY
EOF
  if [ "$isolated" = true ]; then
    cat <<EOF
- model_name: isolated-judge
  litellm_params:
    model: anthropic/$judge
    api_key: os.environ/LAB_JUDGE_ANTHROPIC_API_KEY
EOF
  fi
  cat <<EOF
litellm_settings:
  callbacks:
  - audit_log.audit_logger_instance
  - prometheus
  require_auth_for_metrics_endpoint: false
guardrails:
- guardrail_name: tool-call-review
  litellm_params:
    guardrail: tool_call_review.ToolCallReview
    mode:
EOF
  [ "$content" = true ] && echo "    - pre_call"
  cat <<EOF
    - post_call
    default_on: true
    use_jev: false
    jev_model: typesafe/jev-latest
    jev_threshold: ${LAB_JEV_THRESHOLD:-0.2}
    judge_model: review-judge
    fail_open: false
    test_model: fake-tools
    test_judge_model: fake-tools
    isolated_review: $isolated
    context_review_claude_code: $(lower "${LAB_CONTEXT_REVIEW_CLAUDE_CODE:-false}")
EOF
  if [ "$isolated" = true ]; then
    cat <<EOF
    isolated_review_claude_code: $(lower "${LAB_ISOLATED_CLAUDE_CODE:-true}")
    isolated_judge_model: isolated-judge
    isolated_policy: |-
$(policy_text | indent '      ')
    isolated_fail_open: false
    test_isolated_judge_model: fake-tools
    isolated_content: $content
    isolated_content_max_bytes: ${LAB_ISOLATED_CONTENT_MAX_BYTES:-24576}
    isolated_content_max_files: ${LAB_ISOLATED_CONTENT_MAX_FILES:-5}
    isolated_destinations:
EOF
    dests="${LAB_ISOLATED_DESTINATIONS:-private}"
    printf '%s\n' "$dests" | tr ',' '\n' | while IFS= read -r d; do
      d=$(printf '%s' "$d" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'); [ -n "$d" ] && echo "    - $(yaml_str "$d")"
    done
  fi
  if [ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" ] && [ -n "${GOOGLE_OAUTH_CLIENT_SECRET:-}" ]; then
    cat <<EOF
mcp_servers:
  gmail:
    url: http://workspace-mcp:8000/mcp
    transport: http
    auth_type: oauth2
    oauth2_flow: authorization_code
    client_id: os.environ/GOOGLE_OAUTH_CLIENT_ID
    client_secret: os.environ/GOOGLE_OAUTH_CLIENT_SECRET
    authorization_url: https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent
    token_url: https://oauth2.googleapis.com/token
    scopes:
    - openid
    - https://www.googleapis.com/auth/userinfo.email
    - https://www.googleapis.com/auth/userinfo.profile
    - https://www.googleapis.com/auth/gmail.readonly
    oauth_identity_binding:
      mode: enforce
      issuer: https://accounts.google.com
      audiences:
      - $(yaml_str "$GOOGLE_OAUTH_CLIENT_ID")
      principal_claim: email
      caller_field: user_email
      require_email_verified: true
EOF
  fi
  cat <<EOF
router_settings:
  routing_strategy: simple-shuffle
  disable_cooldowns: true
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  database_connection_pool_limit: $pool_limit
  proxy_batch_write_at: 60
  use_redis_transaction_buffer: true
  require_key_mcp_access_defined: true
  disable_spend_logs: true
EOF
}

# ---------------------------------------------------------------- 秘密の値（S3 の環境変数のファイル）
write_env_files() {
  say "設定と秘密の値を置く"
  local d="$BUILD/env"; mkdir -p "$d"; chmod 700 "$d"
  {
    echo "LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY"
    echo "LITELLM_SALT_KEY=$LITELLM_SALT_KEY"
    echo "DATABASE_PASSWORD=$POSTGRES_PASSWORD"
    echo "REDIS_PASSWORD=$REDIS_PASSWORD"
    echo "REDIS_SENTINEL_PASSWORD=$SENTINEL_PASSWORD"
    echo "LAB_JUDGE_ANTHROPIC_API_KEY=${LAB_JUDGE_ANTHROPIC_API_KEY:-}"
    if [ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" ]; then
      echo "GOOGLE_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID"; echo "GOOGLE_CLIENT_SECRET=$GOOGLE_OAUTH_CLIENT_SECRET"
      echo "GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID"; echo "GOOGLE_OAUTH_CLIENT_SECRET=$GOOGLE_OAUTH_CLIENT_SECRET"
    fi
    [ -z "${LAB_PROXY_ADMIN_ID:-}" ] || echo "PROXY_ADMIN_ID=$LAB_PROXY_ADMIN_ID"
    [ -z "${LAB_ALLOWED_EMAIL_DOMAINS:-}" ] || echo "ALLOWED_EMAIL_DOMAINS=$LAB_ALLOWED_EMAIL_DOMAINS"
  } > "$d/litellm.env"
  { echo "DATABASE_PASSWORD=$POSTGRES_PASSWORD"; } > "$d/pgbouncer.env"
  { echo "REDIS_PASSWORD=$REDIS_PASSWORD"; echo "SENTINEL_PASSWORD=$SENTINEL_PASSWORD"; } > "$d/valkey.env"
  {
    echo "AUDIT_SESSION_SECRET=$AUDIT_SESSION_SECRET"
    echo "AUDIT_OIDC_CLIENT_ID=${LAB_AUDIT_OIDC_CLIENT_ID:-${GOOGLE_OAUTH_CLIENT_ID:-}}"
    echo "AUDIT_OIDC_CLIENT_SECRET=${LAB_AUDIT_OIDC_CLIENT_SECRET:-${GOOGLE_OAUTH_CLIENT_SECRET:-}}"
  } > "$d/viewer.env"
  { echo "GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID:-}"; echo "GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET:-}"; } > "$d/mcp.env"
  chmod 600 "$d"/*.env
  local n h
  ENV_KEYS=""
  for n in litellm pgbouncer valkey viewer mcp; do
    h=$(hash_of < "$d/$n.env")
    put_config "env/$n-$h.env" "$d/$n.env"
    ENV_KEYS="$ENV_KEYS $n=env/$n-$h.env"
  done
  render_config > "$BUILD/litellm.yaml"
  h=$(hash_of < "$BUILD/litellm.yaml")
  put_config "litellm/$h.yaml" "$BUILD/litellm.yaml"
  LITELLM_CONFIG_KEY="litellm/$h.yaml"
  if [ "$INFRA" = managed ]; then
    cat "$BUILD/relay-ca.key" "$BUILD/relay-ca.pem" > "$BUILD/relay-mitmproxy-ca.pem"; chmod 600 "$BUILD/relay-mitmproxy-ca.pem"
    put_config relay/mitmproxy-ca.pem "$BUILD/relay-mitmproxy-ca.pem"
  fi
  local ca; ca=$(egress_ca_file)
  if [ -n "$ca" ] && [ -s "$ca" ]; then put_config relay/egress-ca.pem "$ca"; fi
  ok "設定: $LITELLM_CONFIG_KEY"
}
env_key() { printf '%s\n' $ENV_KEYS | sed -n "s/^$1=//p"; }

# ---------------------------------------------------------------- CloudFormation
no_proxy_list() {
  printf 'localhost,127.0.0.1,169.254.169.254,169.254.170.2,.%s,.amazonaws.com,pgbouncer,fake-llm,workspace-mcp,%s' "$NS" "$(vpc_cidr)"
}
vpc_cidr() {
  if [ "$INFRA" = managed ]; then printf '%s' "${LAB_ECS_VPC_CIDR:-10.70.0.0/16}"
  else awsc ec2 describe-vpcs --vpc-ids "$LAB_ECS_VPC_ID" --query 'Vpcs[0].CidrBlock' --output text; fi
}
operator_cidr() {
  if [ -n "${LAB_ECS_OPERATOR_CIDR:-}" ]; then printf '%s' "$LAB_ECS_OPERATOR_CIDR"; return; fi
  printf '%s/32' "$(env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy curl -s --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
}

deploy_stack() {  # 引数: LiteLLM のイメージ 移行のイメージ LiteLLM を動かすか
  local img_litellm="$1" img_migrate="$2" enabled="$3" egress no_proxy subnets="" zabbix_server
  render_template > "$BUILD/template.yaml"
  if [ "$INFRA" = managed ]; then egress="http://relay.$NS:3128"; else egress="${LAB_EGRESS_PROXY:-}"; fi
  no_proxy=$(no_proxy_list)
  [ "$INFRA" = provided ] && subnets="$LAB_ECS_SUBNET_IDS"
  zabbix_server="${LAB_ZABBIX_SERVER:-}"
  local params=(
    "Prefix=$PREFIX" "VpcCidr=$(vpc_cidr)" "VpcId=${LAB_ECS_VPC_ID:-}" "SubnetIds=$subnets"
    "OperatorCidr=$(operator_cidr)" "BastionCidr=${LAB_ECS_BASTION_CIDR:-}" "ClientCidr=${LAB_ECS_CLIENT_CIDR:-}"
    "CertificateArn=$CERT_ARN" "BaseUrl=https://$HOSTNAME_"
    "ImageLitellm=$img_litellm" "ImageMigrate=$img_migrate" "ImageAudit=$IMG_AUDIT" "ImageFakeLlm=$IMG_FAKE"
    "ImageValkey=$IMG_VALKEY" "ImagePgbouncer=$IMG_PGBOUNCER" "ImageMcp=$IMG_MCP"
    "ConfigBucket=$(bucket config)" "LitellmConfigKey=$LITELLM_CONFIG_KEY"
    "EnvLitellm=$(env_key litellm)" "EnvPgbouncer=$(env_key pgbouncer)" "EnvValkey=$(env_key valkey)"
    "EnvViewer=$(env_key viewer)" "EnvMcp=$(env_key mcp)"
    "LitellmEnabled=$enabled" "LitellmCpu=${LAB_LITELLM_CPU:-1024}" "LitellmMemory=${LAB_LITELLM_MEMORY:-8192}"
    "LitellmMin=${LAB_LITELLM_MIN:-2}" "LitellmMax=${LAB_LITELLM_MAX:-30}"
    "PgVersion=${LAB_ECS_PG_VERSION:-17.9}" "RdsClass=${LAB_ECS_RDS_CLASS:-db.m7g.large}" "RdsStorage=${LAB_ECS_RDS_STORAGE_GB:-100}" "PostgresPassword=$POSTGRES_PASSWORD"
    "PoolerMode=${LAB_DB_POOLER:-pooler}" "EgressProxy=$egress" "NoProxy=$no_proxy"
    "LockMode=$( [ "$SCALE" = prod ] && echo COMPLIANCE || echo "${LAB_AUDIT_LOCK_MODE:-GOVERNANCE}")"
    "RetentionDays=${LAB_AUDIT_RETENTION_DAYS:-90}" "AccessRetentionDays=${LAB_AUDIT_ACCESS_RETENTION_DAYS:-90}"
    "RetentionExpireDays=$(( ${LAB_AUDIT_RETENTION_DAYS:-90} + 1 ))" "AccessExpireDays=$(( ${LAB_AUDIT_ACCESS_RETENTION_DAYS:-90} + 1 ))"
    "ZabbixServer=$zabbix_server" "MonitorType=${LAB_ECS_MONITOR_TYPE:-t3.medium}"
    "HasGoogle=$( [ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" ] && echo true || echo false)" "FakeLlm=${LAB_ECS_FAKE_LLM:-true}"
    "AuditAdmins=${LAB_AUDIT_ADMINS:-${LAB_USER1_EMAIL:-}}" "AuditAuditors=${LAB_AUDITORS:-}"
    "ZabbixWebPort=8080"
  )
  say "CloudFormation（${STACK}）"
  local st; st=$(awsc cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)
  case "$st" in *ROLLBACK_IN_PROGRESS) awsc cloudformation wait stack-rollback-complete --stack-name "$STACK" 2>/dev/null || true
                                      st=$(awsc cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text) ;; esac
  if [ "$st" = ROLLBACK_COMPLETE ]; then   # 作成に失敗したスタックは更新できないので消して作り直す
    warn "作成に失敗したスタックを消して作り直します"
    awsc cloudformation delete-stack --stack-name "$STACK"
    awsc cloudformation wait stack-delete-complete --stack-name "$STACK"
    local n   # 作成の途中で作られた空のバケット（Retain のため残る）を消す。中身があれば消さない
    for n in audit-raw audit-access audit-derived; do
      if [ "$(awsc s3api list-object-versions --bucket "$(bucket "$n")" --query 'length(Versions || `[]`)' --output text 2>/dev/null)" = 0 ]; then
        awsc s3api delete-bucket --bucket "$(bucket "$n")" && ok "空のバケットを消しました: $(bucket "$n")"
      fi
    done
  fi
  awsc cloudformation deploy --stack-name "$STACK" --template-file "$BUILD/template.yaml" \
    --s3-bucket "$(bucket config)" --s3-prefix cfn --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-fail-on-empty-changeset --parameter-overrides "${params[@]}" >&2 || {
      awsc cloudformation describe-stack-events --stack-name "$STACK" --max-items 30 \
        --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output text >&2 || true
      die "スタックの配備に失敗しました"; }
  ok "スタック: $STACK"
  wait_relay
}
wait_relay() {  # 端末が検証の中継を通しているとき、スタックの更新で中継の台が作り直されても、戻るまで待つ（2026-11-22 に判明）
  [ -n "${OPERATOR_RELAY:-}" ] || return 0
  local i
  for i in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 10 -x "$OPERATOR_RELAY" "https://sts.$REGION.amazonaws.com/" 2>/dev/null; then return 0; fi
    [ "$i" = 1 ] && say "中継の台が作り直されたので、中継が戻るのを待ちます"
    sleep 10
  done
  die "中継（$OPERATOR_RELAY）が戻りません"
}

# ---------------------------------------------------------------- DB 移行
migrate() {  # イメージのタグと RDS の DbiResourceId から印を作り、印があれば流さない（今の Job の名前のハッシュと同じ考え方）
  local img="$1" mark cluster td subnets sg task code
  mark="migrate/$(printf '%s %s' "$img" "$(stack_out RdsResourceId)" | hash_of)"
  if awsc s3api head-object --bucket "$(bucket config)" --key "$mark" >/dev/null 2>&1; then ok "DB 移行は済んでいます"; return; fi
  say "DB 移行（単発のタスク）"
  cluster=$(stack_out ClusterName); td=$(stack_out MigrateTaskDefinition)
  subnets=$(stack_out PrivateSubnets); sg=$(stack_out MigrateSecurityGroup)
  task=$(awsc ecs run-task --cluster "$cluster" --task-definition "$td" --launch-type FARGATE \
           --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=DISABLED}" \
           --query 'tasks[0].taskArn' --output text)
  awsc ecs wait tasks-stopped --cluster "$cluster" --tasks "$task" 2>/dev/null || awsc ecs wait tasks-stopped --cluster "$cluster" --tasks "$task"
  code=$(awsc ecs describe-tasks --cluster "$cluster" --tasks "$task" --query 'tasks[0].containers[0].exitCode' --output text)
  if [ "$code" != 0 ]; then
    awsc logs tail "/$PREFIX/litellm-migrate" --since 15m 2>/dev/null | tail -40 >&2 || true
    die "DB 移行が失敗しました（終了コード ${code}）"
  fi
  printf 'ok\n' > "$BUILD/migrate-mark"; put_config "$mark" "$BUILD/migrate-mark"
  ok "DB 移行が終わりました"
}

wait_stable() {
  local cluster svcs
  cluster=$(stack_out ClusterName)
  svcs=$(awsc ecs list-services --cluster "$cluster" --query 'serviceArns' --output text)
  say "サービスの安定を待ちます"
  # 一度に待てるのは 10 個まで
  local batch
  while [ -n "$(printf '%s' "$svcs" | tr -d '[:space:]')" ]; do
    batch=$(printf '%s\n' $svcs | head -10 | tr '\n' ' ')
    svcs=$(printf '%s\n' $svcs | tail -n +11 | tr '\n' ' ')
    # shellcheck disable=SC2086
    awsc ecs wait services-stable --cluster "$cluster" --services $batch || die "サービスが安定しません（ecs-lab.sh status で確かめてください）"
  done
  ok "全サービスが安定しました"
}

billing_json() {  # contracts / fx_rates の初期値（labtool/render.py の initial_contracts と initial_fx_rates の VM 構成と同じ中身）
  local month; month=$(TZ=Asia/Tokyo date +%Y-%m)
  if [ "$1" = contracts ]; then
    if [ -n "${LAB_CONTRACTS:-}" ]; then printf '%s' "$LAB_CONTRACTS"; else
      printf '{"schema": 1, "contracts": [{"id": "claude-max", "name": "Claude Max", "kind": "subscription", "currency": "USD", "models": ["anthropic/*", "claude-*"], "fees": {"%s": 100.0}}, {"id": "anthropic-api", "name": "Anthropic API", "kind": "invoice", "currency": "USD", "models": ["review-judge", "isolated-judge"], "fees": {}}]}' "$month"
    fi
  else
    if [ -n "${LAB_FX_RATES:-}" ]; then printf '%s' "$LAB_FX_RATES"; else printf '%s' '{"schema": 1, "usd_jpy": {}}'; fi
  fi
}
initial_billing() {  # 請求の単位と換算のレートの初期値（無いときだけ）
  local b k
  b=$(bucket audit-derived)
  for k in contracts fx_rates; do
    if [ "$(awsc s3api list-objects-v2 --bucket "$b" --prefix "billing/$k/" --query 'length(Contents || `[]`)' --output text)" = 0 ]; then
      billing_json "$k" > "$BUILD/$k.json"
      awsc s3api put-object --bucket "$b" --key "billing/$k/00000000T000000-lab-initial.json" --body "$BUILD/$k.json" >/dev/null
      ok "$k の初期値を書きました"
    fi
  done
}

# ---------------------------------------------------------------- 監視の台での実行（SSM）
monitor_instance() {
  local id; id=$(stack_out MonitorInstanceId)
  if [ -z "$id" ]; then
    id=$(awsc ec2 describe-instances --filters "Name=tag:lab:role,Values=$PREFIX-monitor" "Name=instance-state-name,Values=running" \
           --query 'Reservations[0].Instances[0].InstanceId' --output text | sed 's/^None$//')
  fi
  printf '%s' "$id"
}
on_monitor() {  # シェルの文: 監視の台で流し、標準出力を返す（VPC の中から入口を叩く疎通の確認に使う）
  local id cmd status out i params
  id=$(monitor_instance); [ -n "$id" ] || die "監視の台がありません"
  params=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
  cmd=$(awsc ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript \
          --parameters "{\"commands\":[\"$params\"]}" --query Command.CommandId --output text)
  for i in $(seq 1 60); do
    sleep 2
    status=$(awsc ssm get-command-invocation --command-id "$cmd" --instance-id "$id" --query Status --output text 2>/dev/null || true)
    case "$status" in Success|Failed|TimedOut|Cancelled) break ;; esac
  done
  out=$(awsc ssm get-command-invocation --command-id "$cmd" --instance-id "$id" --query StandardOutputContent --output text)
  printf '%s\n' "$out"
  [ "$status" = Success ]
}
wait_monitor_ready() {
  local id i
  id=$(monitor_instance); [ -n "$id" ] || return 0
  for i in $(seq 1 90); do
    if [ "$(awsc ssm describe-instance-information --filters "Key=InstanceIds,Values=$id" --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)" = Online ]; then
      if on_monitor 'test -f /var/lib/lab/ready && echo ready' 2>/dev/null | grep -q ready; then ok "監視の台の準備ができました"; return; fi
    fi
    sleep 10
  done
  die "監視の台の初期化が終わりません（/var/log/lab-monitor-init.log）"
}

smoke() {
  say "疎通の確認（監視の台から、VPC の中で入口を呼ぶ）"
  local base="https://$HOSTNAME_" key="$LITELLM_MASTER_KEY" out
  out=$(on_monitor "curl -s -o /dev/null -w '%{http_code}' --max-time 20 $base/health/readiness") || true
  [ "$(printf '%s' "$out" | tail -1)" = 200 ] || die "入口の readiness が 200 ではありません（${out}）"
  ok "入口の readiness: 200"
  out=$(on_monitor "curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H 'Authorization: Bearer $key' $base/v1/models") || true
  [ "$(printf '%s' "$out" | tail -1)" = 200 ] || die "/v1/models が 200 ではありません（${out}）"
  ok "/v1/models: 200"
  if is_true "${LAB_ECS_FAKE_LLM:-true}"; then
    local before after b
    b=$(bucket audit-raw)
    before=$(awsc s3api list-objects-v2 --bucket "$b" --prefix raw/ --query 'length(Contents || `[]`)' --output text)
    out=$(on_monitor "curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'Authorization: Bearer $key' -H 'Content-Type: application/json' -d '{\"model\":\"fake-tools\",\"messages\":[{\"role\":\"user\",\"content\":\"smoke\"}]}' $base/v1/chat/completions") || true
    [ "$(printf '%s' "$out" | tail -1)" = 200 ] || die "fake-tools への要求が 200 ではありません（${out}）"
    ok "fake-tools への要求: 200"
    for i in $(seq 1 12); do
      sleep 5
      after=$(awsc s3api list-objects-v2 --bucket "$b" --prefix raw/ --query 'length(Contents || `[]`)' --output text)
      [ "$after" -gt "$before" ] && { ok "監査ログの原本が増えました（$before → ${after}）"; return; }
    done
    die "監査ログの原本が増えません"
  fi
}

forward() {
  command -v session-manager-plugin >/dev/null || die "Session Manager のプラグインがありません（brew install --cask session-manager-plugin）"
  local id alb
  id=$(monitor_instance); alb=$(stack_out AlbDns)
  say "転送: https://$(local_hostname):$LOCAL_PORT → 入口（${alb}）。Zabbix の web は http://127.0.0.1:$ZABBIX_LOCAL_PORT/"
  awsc ssm start-session --target "$id" --document-name AWS-StartPortForwardingSession \
    --parameters "portNumber=8080,localPortNumber=$ZABBIX_LOCAL_PORT" >/dev/null 2>&1 &
  trap 'kill %1 2>/dev/null || true' EXIT
  awsc ssm start-session --target "$id" --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "host=$alb,portNumber=443,localPortNumber=$LOCAL_PORT"
}

# ---------------------------------------------------------------- 命令
cmd_up() {
  preflight
  ensure_relay_ca
  # 2 回目以降は、検証の中継が既にあれば端末の通信もそこに通す（社内プロキシの中の端末と同じ条件）
  if [ "$INFRA" = managed ] && stack_exists && is_true "${LAB_ECS_OPERATOR_VIA_RELAY:-true}"; then
    local ip; ip=$(stack_out MonitorPublicIp)
    if [ -n "$ip" ]; then apply_operator_proxy "http://$ip:3128" "$BUILD/relay-ca.pem"; OPERATOR_RELAY="http://$ip:3128"; fi
  elif [ -n "${LAB_EGRESS_PROXY_MAC:-}" ]; then
    apply_operator_proxy "$LAB_EGRESS_PROXY_MAC" "${LAB_EGRESS_CA_FILE:-}"
  fi
  ensure_config_bucket
  ensure_certificate
  build_images
  write_env_files
  # LiteLLM は、DB 移行が済むまで今の版のまま（初回は 0 タスク）にし、移行の後で新しい版にする
  local current enabled=false
  current=""
  if stack_exists; then current=$(stack_param ImageLitellm); [ "$(stack_param LitellmEnabled)" = true ] && enabled=true; fi
  [ -n "$current" ] || current="$IMG_LITELLM"
  deploy_stack "$current" "$IMG_LITELLM" "$enabled"
  migrate "$IMG_LITELLM"
  deploy_stack "$IMG_LITELLM" "$IMG_LITELLM" true
  wait_stable
  # 端末の試験（./lab の LAB_TARGET=aws-ecs）が通る中継（検証だけ）
  if [ "$INFRA" = managed ]; then printf 'http://%s:3128\n' "$(stack_out MonitorPublicIp)" > "$BUILD/relay-proxy"; fi
  cf_upsert CNAME "$HOSTNAME_" "$(stack_out AlbDns)"
  initial_billing
  wait_monitor_ready
  zabbix_register
  smoke
  ok "完了: https://${HOSTNAME_}（VPC の中から）。端末からは ecs/ecs-lab.sh forward で https://$(local_hostname):$LOCAL_PORT"
}

empty_bucket() {  # 版と削除マーカーを全部消す（GOVERNANCE の保持は回避する。COMPLIANCE は消せない）
  local b="$1" batch bypass=()
  awsc s3api head-bucket --bucket "$b" >/dev/null 2>&1 || return 0
  # 保持の回避は Object Lock のあるバケットにだけ付けられる（無いバケットに付けると InvalidArgument。2026-11-22 に判明）
  if awsc s3api get-object-lock-configuration --bucket "$b" >/dev/null 2>&1; then bypass=(--bypass-governance-retention); fi
  while :; do
    batch=$(awsc s3api list-object-versions --bucket "$b" --max-items 500 \
              --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers[].{Key:Key,VersionId:VersionId}][]}' --output json)
    printf '%s' "$batch" | grep -q '"Key"' || break
    printf '%s' "$batch" > "$BUILD/delete.json"
    awsc s3api delete-objects --bucket "$b" "${bypass[@]+"${bypass[@]}"}" --delete "file://$BUILD/delete.json" >/dev/null || return 1
  done
  awsc s3api delete-bucket --bucket "$b" && ok "バケットを消しました: $b"
}
cmd_down() {
  preflight
  say "削除（${STACK}）"
  # 共有の Zabbix（provided の既存サーバー）から、この環境のホストを外す。検証のサーバーは台と一緒に消える
  if [ -n "${LAB_ZABBIX_SERVER:-}" ] && stack_exists && [ -n "$(monitor_instance)" ]; then
    on_monitor 'lab-s3get zabbix/register.py /usr/local/lib/lab-zabbix-register.py && python3 /usr/local/lib/lab-zabbix-register.py remove 2>&1' >&2 \
      || warn "Zabbix から外せませんでした（既存のサーバーで ${PREFIX}-* のホストを消してください）"
  fi
  cf_delete CNAME "$HOSTNAME_" || true
  cf_delete A "$(local_hostname)" || true
  if stack_exists; then
    awsc cloudformation delete-stack --stack-name "$STACK"
    awsc cloudformation wait stack-delete-complete --stack-name "$STACK" || die "スタックの削除に失敗しました"
    ok "スタックを消しました"
  fi
  local n kept=""
  for n in audit-raw audit-access audit-derived; do
    empty_bucket "$(bucket "$n")" || kept="$kept $(bucket "$n")"
  done
  for n in $REPOS; do
    awsc ecr delete-repository --repository-name "$PREFIX/$n" --force >/dev/null 2>&1 && ok "ECR を消しました: $PREFIX/$n" || true
  done
  empty_bucket "$(bucket config)" || kept="$kept $(bucket config)"
  [ -z "$kept" ] || warn "消せなかったバケット（COMPLIANCE の保持など）:$kept"
  ok "削除が終わりました（ACM の証明書は無料なので残します）"
}
cmd_status() {
  stack_exists || { warn "スタックがありません"; return; }
  local cluster
  cluster=$(stack_out ClusterName)
  awsc cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text
  awsc ecs describe-services --cluster "$cluster" --services $(awsc ecs list-services --cluster "$cluster" --query serviceArns --output text) \
    --query 'services[].[serviceName,desiredCount,runningCount,deployments[0].rolloutState]' --output table
  awsc rds describe-db-instances --db-instance-identifier "$(stack_out RdsId)" \
    --query 'DBInstances[0].[DBInstanceStatus,MultiAZ,AvailabilityZone,SecondaryAvailabilityZone]' --output text
  awsc elbv2 describe-target-health --target-group-arn "$(stack_out LitellmTargetGroup)" \
    --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text
}

# ---------------------------------------------------------------- Zabbix の登録（9 章。埋め込んだ YAML を configuration.import で取り込む）
zbx_call() {  # METHOD PARAMS_JSON → 応答の JSON（API の URL とトークンは zabbix_api_url / ZBX_TOKEN）
  local body; body=$(printf '{"jsonrpc":"2.0","method":"%s","params":%s,"id":1}' "$1" "$2")
  curl -sS --max-time 60 -H 'Content-Type: application/json-rpc' ${ZBX_TOKEN:+-H "Authorization: Bearer $ZBX_TOKEN"} \
    --data "$body" "$ZBX_URL"
}
zbx_uuid() {  # 鍵の文字列 → UUIDv4 の形の 32 桁（取り込みのたびに同じ値。Zabbix は UUIDv4 を求める）
  local h; h=$(printf '%s' "$1" | openssl md5 | awk '{print $NF}')
  printf '%s4%sa%s' "${h:0:12}" "${h:13:3}" "${h:17}"
}
ZBX_TEMPLATE=litellm-ecs
LITELLM_FAIL_PCT=5
# (キー, 表示名, 指標, 単位)。labtool/zabbix_monitors.py の LITELLM_COUNTERS と同じ
LITELLM_COUNTERS="req|要求の数（毎秒）|litellm_proxy_total_requests_metric_total|req/s
fail|失敗した要求の数（毎秒）|litellm_proxy_failed_requests_metric_total|req/s
latency_sum|待ち時間の合計（毎秒）|litellm_request_total_latency_metric_sum|s
latency_count|待ち時間の数（毎秒）|litellm_request_total_latency_metric_count|
deploy_fail|LLM の送り先の失敗の数（毎秒）|litellm_deployment_failure_responses_total|req/s"

zbx_import_yaml() {  # Zabbix の取り込み形式（configuration.import）の監視の定義。監視の台のホストに付ける
  # 式の中のホストは空（//キー）にして、付けたホストを指す（テンプレート名で書くと、付けたときに置き換わらなかった。2026-11-21）
  local T="/$ZBX_TEMPLATE" C="/" key label metric units
cat <<EOF
zabbix_export:
  version: '7.0'
  template_groups:
    - uuid: $(zbx_uuid group)
      name: Templates/litellm-lab
  templates:
    - uuid: $(zbx_uuid template)
      template: $ZBX_TEMPLATE
      name: $ZBX_TEMPLATE
      description: 'litellm-agent-gateway-lab の ECS 構成の監視（ecs/ecs-lab.sh が取り込む。手で編集しない）'
      groups:
        - name: Templates/litellm-lab
      macros:
        - macro: '{\$LAB.HOST}'
          value: ''
          description: '入口のホスト名（環境ごとにホストのマクロで上書きする。テンプレートは複数の環境で共有する）'
      items:
        - uuid: $(zbx_uuid lab.ecs.services)
          name: 'litellm-ecs: ECS のサービスの一覧'
          key: lab.ecs.services
          delay: 1m
          history: 1h
          value_type: TEXT
          trends: '0'
        - uuid: $(zbx_uuid lab.ecs.tasks)
          name: 'litellm-ecs: LiteLLM のタスクの一覧'
          key: 'lab.ecs.tasks[litellm]'
          delay: 1m
          history: 1h
          value_type: TEXT
          trends: '0'
        - uuid: $(zbx_uuid lab.ecs.valkey)
          name: 'litellm-ecs: Valkey の役割'
          key: lab.ecs.valkey
          delay: 1m
          history: 1h
          value_type: TEXT
          trends: '0'
        - uuid: $(zbx_uuid lab.valkey.primaries)
          name: 'litellm-ecs: Valkey の primary の数'
          type: DEPENDENT
          key: lab.valkey.primaries
          delay: '0'
          value_type: UNSIGNED
          master_item: {key: lab.ecs.valkey}
          preprocessing:
            - type: JSONPATH
              parameters: ['\$.primaries']
          triggers:
            - uuid: $(zbx_uuid trigger.valkey.primaries)
              expression: 'max($T/lab.valkey.primaries,2m)<1 or min($T/lab.valkey.primaries,2m)>1'
              name: 'litellm-ecs: Valkey の primary が 1 台でない状態が 2 分続いている'
              priority: HIGH
        - uuid: $(zbx_uuid lab.valkey.replicas)
          name: 'litellm-ecs: Valkey の replica の数'
          type: DEPENDENT
          key: lab.valkey.replicas
          delay: '0'
          value_type: UNSIGNED
          master_item: {key: lab.ecs.valkey}
          preprocessing:
            - type: JSONPATH
              parameters: ['\$.replicas']
          triggers:
            - uuid: $(zbx_uuid trigger.valkey.replicas)
              expression: 'max($T/lab.valkey.replicas,5m)<2'
              name: 'litellm-ecs: Valkey の replica が 2 台より少ない状態が 5 分続いている'
              priority: WARNING
        - uuid: $(zbx_uuid lab.ecs.compactor)
          name: 'litellm-ecs: 監査ログの整形の最後の成功'
          key: lab.ecs.compactor
          delay: 5m
          value_type: UNSIGNED
          units: unixtime
          triggers:
            - uuid: $(zbx_uuid trigger.compactor)
              expression: 'last($T/lab.ecs.compactor)>0 and now()-last($T/lab.ecs.compactor)>7200'
              name: 'litellm-ecs: 監査ログの整形が 2 時間成功していない'
              priority: WARNING
        - uuid: $(zbx_uuid web.certificate)
          name: 'litellm-ecs: 入口の証明書'
          key: 'web.certificate.get[{\$LAB.HOST},443]'
          delay: 1h
          history: 1d
          value_type: TEXT
          trends: '0'
        - uuid: $(zbx_uuid lab.cert.expires)
          name: 'litellm-ecs: 入口の証明書の期限'
          type: DEPENDENT
          key: lab.cert.expires
          delay: '0'
          value_type: UNSIGNED
          units: unixtime
          master_item: {key: 'web.certificate.get[{\$LAB.HOST},443]'}
          preprocessing:
            - type: JSONPATH
              parameters: ['\$.x509.not_after.timestamp']
          triggers:
            - uuid: $(zbx_uuid trigger.cert)
              expression: 'last($T/lab.cert.expires)-now()<1209600'
              name: 'litellm-ecs: 入口の証明書の期限まで 14 日を切った'
              priority: WARNING
EOF
  while IFS='|' read -r key label metric units; do
cat <<EOF
        - uuid: $(zbx_uuid "lab.litellm.$key.rate")
          name: 'litellm-ecs: LiteLLM の${label}（全タスク）'
          type: CALCULATED
          key: lab.litellm.$key.rate
          delay: 1m
          value_type: FLOAT
          units: '$units'
          params: 'sum(last_foreach($C/lab.litellm.$key.rate[*]))'
EOF
  done <<< "$LITELLM_COUNTERS"
cat <<EOF
        - uuid: $(zbx_uuid lab.litellm.fail.pct)
          name: 'litellm-ecs: LiteLLM の失敗した要求の割合'
          type: CALCULATED
          key: lab.litellm.fail.pct
          delay: 1m
          value_type: FLOAT
          units: '%'
          params: '100*last($C/lab.litellm.fail.rate)/(last($C/lab.litellm.req.rate)+0.0001)'
          triggers:
            - uuid: $(zbx_uuid trigger.litellm.fail)
              expression: 'min($T/lab.litellm.fail.pct,5m)>$LITELLM_FAIL_PCT'
              name: 'litellm-ecs: LiteLLM の失敗した要求の割合が 5 分続けて $LITELLM_FAIL_PCT% を超えた'
              priority: WARNING
        - uuid: $(zbx_uuid lab.litellm.latency.avg)
          name: 'litellm-ecs: LiteLLM の平均の待ち時間'
          type: CALCULATED
          key: lab.litellm.latency.avg
          delay: 1m
          value_type: FLOAT
          units: s
          params: 'last($C/lab.litellm.latency_sum.rate)/(last($C/lab.litellm.latency_count.rate)+0.0001)'
        - uuid: $(zbx_uuid lab.litellm.deploy_fail.watch)
          name: 'litellm-ecs: LiteLLM の LLM の送り先の失敗（全タスク、警報用）'
          type: CALCULATED
          key: lab.litellm.deploy_fail.watch
          delay: 1m
          value_type: FLOAT
          params: 'last($C/lab.litellm.deploy_fail.rate)'
          triggers:
            - uuid: $(zbx_uuid trigger.litellm.deploy_fail)
              expression: 'min($T/lab.litellm.deploy_fail.watch,5m)>0'
              name: 'litellm-ecs: LiteLLM の LLM の送り先の失敗が 5 分続いている'
              priority: WARNING
      discovery_rules:
        - uuid: $(zbx_uuid discovery.services)
          name: 'litellm-ecs: ECS のサービスの発見'
          type: DEPENDENT
          key: lab.ecs.services.discovery
          delay: '0'
          lifetime_type: DELETE_AFTER
          lifetime: 1d
          master_item: {key: lab.ecs.services}
          preprocessing:
            - type: JAVASCRIPT
              parameters:
                - 'return JSON.stringify(JSON.parse(value).map(function (s) { return {"{#SERVICE}": s.name}; }));'
          item_prototypes:
            - uuid: $(zbx_uuid proto.svc.desired)
              name: 'litellm-ecs: {#SERVICE} の予定のタスクの数'
              type: DEPENDENT
              key: 'lab.ecs.svc.desired[{#SERVICE}]'
              delay: '0'
              value_type: UNSIGNED
              master_item: {key: lab.ecs.services}
              preprocessing:
                - type: JSONPATH
                  parameters: ['\$[?(@.name=="{#SERVICE}")].desired.first()']
            - uuid: $(zbx_uuid proto.svc.running)
              name: 'litellm-ecs: {#SERVICE} の動いているタスクの数'
              type: DEPENDENT
              key: 'lab.ecs.svc.running[{#SERVICE}]'
              delay: '0'
              value_type: UNSIGNED
              master_item: {key: lab.ecs.services}
              preprocessing:
                - type: JSONPATH
                  parameters: ['\$[?(@.name=="{#SERVICE}")].running.first()']
          trigger_prototypes:
            - uuid: $(zbx_uuid proto.trigger.svc)
              expression: 'max($T/lab.ecs.svc.running[{#SERVICE}],5m)<last($T/lab.ecs.svc.desired[{#SERVICE}])'
              name: 'litellm-ecs: {#SERVICE} の動いているタスクが予定より少ない状態が 5 分続いている'
              priority: WARNING
        - uuid: $(zbx_uuid discovery.tasks)
          name: 'litellm-ecs: LiteLLM のタスクの発見'
          type: DEPENDENT
          key: lab.ecs.tasks.discovery
          delay: '0'
          lifetime_type: DELETE_AFTER
          lifetime: 1h
          enabled_lifetime_type: DISABLE_IMMEDIATELY
          master_item: {key: 'lab.ecs.tasks[litellm]'}
          item_prototypes:
            - uuid: $(zbx_uuid proto.metrics)
              name: '{#TASK}: LiteLLM の指標（/metrics）'
              type: HTTP_AGENT
              key: 'lab.litellm.metrics[{#TASK}]'
              delay: 1m
              history: '0'
              value_type: TEXT
              trends: '0'
              timeout: 10s
              url: 'http://{#TASKIP}:4000/metrics'
EOF
  while IFS='|' read -r key label metric units; do
cat <<EOF
            - uuid: $(zbx_uuid "proto.$key")
              name: '{#TASK}: $label'
              type: DEPENDENT
              key: 'lab.litellm.$key.rate[{#TASK}]'
              delay: '0'
              value_type: FLOAT
              units: '$units'
              master_item: {key: 'lab.litellm.metrics[{#TASK}]'}
              preprocessing:
                - type: PROMETHEUS_PATTERN
                  parameters: ['$metric', function, sum]
                  error_handler: CUSTOM_VALUE
                  error_handler_params: '0'
                - type: CHANGE_PER_SECOND
                  parameters: ['']
EOF
  done <<< "$LITELLM_COUNTERS"
}

zbx_helper_py() {  # 監視の台の補助（Zabbix agent 2 の UserParameter から呼ぶ）。標準ライブラリだけ、資格情報はインスタンスのロール
cat <<'EOF'
#!/usr/bin/env python3
"""litellm-ecs の監視の補助: ECS のサービスとタスク、Valkey の役割、整形の最後の成功を JSON で返す（ecs/ecs-lab.sh が置く）。"""
import datetime, hashlib, hmac, json, socket, sys, urllib.request
CONF = dict(l.strip().split("=", 1) for l in open("/etc/lab/zabbix-agent.env") if "=" in l)
REGION, CLUSTER, NS = CONF["REGION"], CONF["CLUSTER"], CONF["NS"]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # AWS へは VPC エンドポイントで（社内プロキシを通さない）

def creds():
    tok = OPENER.open(urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                      headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}), timeout=3).read().decode()
    h = {"X-aws-ec2-metadata-token": tok}
    base = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    role = OPENER.open(urllib.request.Request(base, headers=h), timeout=3).read().decode().strip()
    return json.loads(OPENER.open(urllib.request.Request(base + role, headers=h), timeout=3).read())

def signed(method, service, host, path="/", body=b"", headers=None, query=""):
    c = creds()
    now = datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(body).hexdigest()
    hdr = {"host": host, "x-amz-date": amz, "x-amz-security-token": c["Token"], "x-amz-content-sha256": payload, **(headers or {})}
    names = sorted(hdr)
    canon = "\n".join([method, path, query, "".join(f"{k}:{hdr[k]}\n" for k in names), ";".join(names), payload])
    scope = f"{day}/{REGION}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = ("AWS4" + c["SecretAccessKey"]).encode()
    for part in (day, REGION, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    hdr["Authorization"] = f"AWS4-HMAC-SHA256 Credential={c['AccessKeyId']}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    url = f"https://{host}{path}" + (f"?{query}" if query else "")
    return OPENER.open(urllib.request.Request(url, data=body or None, method=method, headers=hdr), timeout=10)

def ecs(action, params):
    body = json.dumps(params).encode()
    r = signed("POST", "ecs", f"ecs.{REGION}.amazonaws.com", body=body, headers={
        "content-type": "application/x-amz-json-1.1", "x-amz-target": f"AmazonEC2ContainerServiceV20141113.{action}"})
    return json.loads(r.read())

def services():
    arns, token = [], None
    while True:
        out = ecs("ListServices", {"cluster": CLUSTER, **({"nextToken": token} if token else {})})
        arns += out.get("serviceArns", []); token = out.get("nextToken")
        if not token: break
    res = []
    for i in range(0, len(arns), 10):
        for s in ecs("DescribeServices", {"cluster": CLUSTER, "services": arns[i:i + 10]})["services"]:
            res.append({"name": s["serviceName"], "desired": s["desiredCount"], "running": s["runningCount"], "pending": s["pendingCount"]})
    return res

def tasks(service):
    arns = ecs("ListTasks", {"cluster": CLUSTER, "serviceName": service, "desiredStatus": "RUNNING"}).get("taskArns", [])
    out = []
    for i in range(0, len(arns), 100):
        for t in ecs("DescribeTasks", {"cluster": CLUSTER, "tasks": arns[i:i + 100]})["tasks"]:
            if t.get("lastStatus") != "RUNNING": continue
            ip = next((d["value"] for a in t.get("attachments", []) for d in a.get("details", []) if d["name"] == "privateIPv4Address"), None)
            if ip: out.append({"{#TASK}": t["taskArn"].rsplit("/", 1)[-1][:12], "{#TASKIP}": ip})
    return out

def valkey():
    pw = open("/etc/lab/valkey.pass").read().strip()
    roles = {}
    for i in range(3):
        host = f"valkey-{i}.{NS}"
        try:
            with socket.create_connection((host, 6379), timeout=3) as s:
                s.sendall(f"*2\r\n$4\r\nAUTH\r\n${len(pw)}\r\n{pw}\r\n*2\r\n$4\r\nINFO\r\n$11\r\nreplication\r\n".encode())
                data = b""
                while b"role:" not in data or not data.endswith(b"\r\n\r\n"):
                    chunk = s.recv(65536)
                    if not chunk: break
                    data += chunk
            roles[host] = next((l.split(":", 1)[1].strip() for l in data.decode().splitlines() if l.startswith("role:")), "unknown")
        except OSError:
            roles[host] = "down"
    return {"primaries": sum(r == "master" for r in roles.values()), "replicas": sum(r == "slave" for r in roles.values()), "nodes": roles}

def compactor():
    host = f"{CONF['DERIVED_BUCKET']}.s3.{REGION}.amazonaws.com"
    try:
        r = signed("HEAD", "s3", host, path="/_state/compactor.json")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404): return 0
        raise
    return int(datetime.datetime.strptime(r.headers["Last-Modified"], "%a, %d %b %Y %H:%M:%S GMT")
               .replace(tzinfo=datetime.timezone.utc).timestamp())

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "services": print(json.dumps(services()))
    elif cmd == "tasks": print(json.dumps(tasks(sys.argv[2])))
    elif cmd == "valkey": print(json.dumps(valkey()))
    elif cmd == "compactor": print(compactor())
    else: sys.exit("unknown command")
EOF
}

zbx_register_py() {  # Zabbix の API に登録する（監視の台の上で流す。標準ライブラリだけ）
cat <<'EOF'
#!/usr/bin/env python3
"""litellm-ecs の監視を Zabbix に登録する（ecs/ecs-lab.sh が SSM で流す。何度流しても同じ結果）。"""
import json, sys, time, urllib.request
S = dict(l.rstrip("\n").split("=", 1) for l in open("/etc/lab/zabbix-register.env") if "=" in l)
URL = S.get("ZBX_API_URL") or "http://127.0.0.1:8080/api_jsonrpc.php"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TOKEN = S.get("ZBX_API_TOKEN", "")

def call(method, params, auth=True):
    hdr = {"Content-Type": "application/json-rpc"}
    if auth and TOKEN: hdr["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    out = json.loads(OPENER.open(urllib.request.Request(URL, data=body, headers=hdr), timeout=120).read())
    if "error" in out: raise RuntimeError(f"{method}: {out['error'].get('data') or out['error']}")
    return out["result"]

def login():
    global TOKEN
    if TOKEN: return
    for _ in range(60):   # web が上がるまで待つ（初回は DB の初期化に数分）
        try: call("apiinfo.version", {}, auth=False); break
        except Exception: time.sleep(5)
    want = S["ZBX_ADMIN_PASSWORD"]
    try:
        TOKEN = call("user.login", {"username": "Admin", "password": want}, auth=False)
    except RuntimeError:
        TOKEN = call("user.login", {"username": "Admin", "password": "zabbix"}, auth=False)
        uid = call("user.get", {"filter": {"username": "Admin"}, "output": ["userid"]})[0]["userid"]
        call("user.update", {"userid": uid, "current_passwd": "zabbix", "passwd": want})
        TOKEN = call("user.login", {"username": "Admin", "password": want}, auth=False)
        print("Admin のパスワードを変えました")

def ensure(obj, find, body, idkey, create_extra=None):
    found = call(f"{obj}.get", {"filter": find, "output": [idkey]})
    if found:
        call(f"{obj}.update", {idkey: found[0][idkey], **body})
        return found[0][idkey]
    return call(f"{obj}.create", {**body, **(create_extra or {})})[f"{idkey}s"][0]

def template_ids(names):
    got = {t["host"]: t["templateid"] for t in call("template.get", {"filter": {"host": names}, "output": ["host"]})}
    missing = [n for n in names if n not in got]
    if missing: print("見つからないテンプレート（付けません）:", ", ".join(missing))
    return [{"templateid": got[n]} for n in names if n in got]

def main():
    login()
    prefix = S["PREFIX"]
    gid = ensure("hostgroup", {"name": [prefix]}, {"name": prefix}, "groupid")
    pid = ensure("proxy", {"name": [f"{prefix}-proxy"]}, {"name": f"{prefix}-proxy", "operating_mode": 0}, "proxyid")
    rules = {k: {"createMissing": True, "updateExisting": True} for k in ("template_groups", "templates")}
    rules.update({k: {"createMissing": True, "updateExisting": True, "deleteMissing": True} for k in ("items", "triggers", "discoveryRules")})
    call("configuration.import", {"format": "yaml", "source": open("/etc/lab/zabbix-import.yaml").read(), "rules": rules})
    aws = {"{$AWS.AUTH_TYPE}": "role_base", "{$AWS.REGION}": S["REGION"]}
    hosts = [
        (f"{prefix}-monitor", ["Linux by Zabbix agent", S["TEMPLATE"]], {"{$LAB.HOST}": S["HOST"]}, True),
        # 自動増減のアラーム（TargetTracking-…）は Application Auto Scaling が自分で使う物で、最小数のときは常に ALARM なので外す
        (f"{prefix}-ecs", ["AWS ECS Cluster by HTTP"], {**aws, "{$AWS.ECS.CLUSTER.NAME}": S["CLUSTER"],
                                                         "{$AWS.ECS.LLD.FILTER.ALARM_NAME.NOT_MATCHES}": "^TargetTracking-"}, False),
        (f"{prefix}-rds", ["AWS RDS instance by HTTP"], {**aws, "{$AWS.RDS.INSTANCE.ID}": S["RDS_ID"]}, False),
        (f"{prefix}-alb", ["AWS ELB Application Load Balancer by HTTP"], {**aws, "{$AWS.ELB.ARN}": S["ALB_ARN"]}, False),
    ]
    for name, templates, macros, agent in hosts:
        body = {"host": name, "groups": [{"groupid": gid}], "templates": template_ids(templates), "monitored_by": 1, "proxyid": pid,
                "macros": [{"macro": k, "value": v} for k, v in macros.items()]}
        found = call("host.get", {"filter": {"host": [name]}, "output": ["hostid"]})
        if found:
            call("host.update", {"hostid": found[0]["hostid"], **body})
        else:
            if agent: body["interfaces"] = [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1", "dns": "", "port": "10050"}]
            call("host.create", body)
        print("ホスト:", name)
    if S.get("LOCAL") == "true":   # 検証のサーバーの既定のホストから、居ない agent のテンプレートを外す（VM 構成と同じ）
        srv = call("host.get", {"filter": {"host": ["Zabbix server"]}, "output": ["hostid"], "selectParentTemplates": ["host"]})
        drop = [t["templateid"] for t in call("template.get", {"filter": {"host": ["Linux by Zabbix agent"]}, "output": ["templateid"]})]
        if srv and drop and any(t["host"] == "Linux by Zabbix agent" for t in srv[0]["parentTemplates"]):
            call("host.update", {"hostid": srv[0]["hostid"], "templates_clear": [{"templateid": drop[0]}]})
    print("Zabbix の登録が終わりました")

def remove():
    """down の前に、この環境のホスト・プロキシ・ホストグループを外す（共有の Zabbix に消した環境を残さない）。テンプレートは共有なので残す。"""
    login()
    prefix = S["PREFIX"]
    names = [f"{prefix}-{x}" for x in ("monitor", "ecs", "rds", "alb")]
    ids = [h["hostid"] for h in call("host.get", {"filter": {"host": names}, "output": ["hostid"]})]
    if ids: call("host.delete", ids)
    for p in call("proxy.get", {"filter": {"name": [f"{prefix}-proxy"]}, "output": ["proxyid"]}):
        call("proxy.delete", [p["proxyid"]])
    for g in call("hostgroup.get", {"filter": {"name": [prefix]}, "output": ["groupid"], "selectHosts": ["hostid"]}):
        if not g["hosts"]: call("hostgroup.delete", [g["groupid"]])
    print(f"Zabbix から外しました（ホスト {len(ids)} 件）")

remove() if sys.argv[1:] == ["remove"] else main()
EOF
}

zbx_setup_sh() {  # 監視の台の準備（root で SSM から流す）
cat <<'EOF'
set -euo pipefail
. /etc/lab/monitor.env
lab-s3get zabbix/lab-ecs-api.py /usr/local/bin/lab-ecs-api && chmod 755 /usr/local/bin/lab-ecs-api
lab-s3get zabbix/agent.env /etc/lab/zabbix-agent.env.new
lab-s3get zabbix/register.env /etc/lab/zabbix-register.env && chmod 600 /etc/lab/zabbix-register.env
lab-s3get zabbix/import.yaml /etc/lab/zabbix-import.yaml
lab-s3get zabbix/register.py /usr/local/lib/lab-zabbix-register.py
grep '^REDIS_PASSWORD=' /etc/lab/zabbix-agent.env.new | cut -d= -f2- | tr -d '\n' > /etc/lab/valkey.pass
grep -v '^REDIS_PASSWORD=' /etc/lab/zabbix-agent.env.new > /etc/lab/zabbix-agent.env && rm -f /etc/lab/zabbix-agent.env.new
chown root:zabbix /etc/lab/valkey.pass /etc/lab/zabbix-agent.env && chmod 640 /etc/lab/valkey.pass /etc/lab/zabbix-agent.env
cat > /etc/zabbix/zabbix_agent2.d/lab.conf <<EOT
Server=127.0.0.1
ServerActive=
Hostname=$PREFIX-monitor
Timeout=20
UserParameter=lab.ecs.services,/usr/local/bin/lab-ecs-api services
UserParameter=lab.ecs.tasks[*],/usr/local/bin/lab-ecs-api tasks \$1
UserParameter=lab.ecs.valkey,/usr/local/bin/lab-ecs-api valkey
UserParameter=lab.ecs.compactor,/usr/local/bin/lab-ecs-api compactor
EOT
sed -i -e 's/^Server=/# Server=/' -e 's/^ServerActive=/# ServerActive=/' -e 's/^Hostname=/# Hostname=/' /etc/zabbix/zabbix_agent2.conf
systemctl restart zabbix-agent2
python3 /usr/local/lib/lab-zabbix-register.py
EOF
}

zabbix_register() {
  say "Zabbix の登録（監視の台から）"
  local d="$BUILD/zabbix" alb_arn; mkdir -p "$d"; chmod 700 "$d"
  alb_arn=$(awsc elbv2 describe-load-balancers --names "$PREFIX-alb" --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  zbx_import_yaml > "$d/import.yaml"
  zbx_helper_py > "$d/lab-ecs-api.py"
  zbx_register_py > "$d/register.py"
  zbx_setup_sh > "$d/setup.sh"
  printf 'REGION=%s\nCLUSTER=%s\nNS=%s\nDERIVED_BUCKET=%s\nREDIS_PASSWORD=%s\n' "$REGION" "$PREFIX" "$NS" "$(bucket audit-derived)" "$REDIS_PASSWORD" > "$d/agent.env"
  printf 'HOST=%s\nPREFIX=%s\nREGION=%s\nCLUSTER=%s\nRDS_ID=%s\nALB_ARN=%s\nTEMPLATE=%s\nZBX_API_URL=%s\nZBX_API_TOKEN=%s\nZBX_ADMIN_PASSWORD=%s\nLOCAL=%s\n' \
    "$HOSTNAME_" "$PREFIX" "$REGION" "$PREFIX" "$(stack_out RdsId)" "$alb_arn" "$ZBX_TEMPLATE" "${LAB_ZABBIX_API_URL:-}" "${LAB_ZABBIX_API_TOKEN:-}" \
    "$LAB_ZABBIX_ADMIN_PASSWORD" "$( [ -z "${LAB_ZABBIX_SERVER:-}" ] && echo true || echo false)" > "$d/register.env"
  chmod 600 "$d"/*
  local f
  for f in import.yaml lab-ecs-api.py register.py setup.sh agent.env register.env; do put_config "zabbix/$f" "$d/$f"; done
  on_monitor 'lab-s3get zabbix/setup.sh /tmp/lab-zabbix-setup.sh && bash /tmp/lab-zabbix-setup.sh 2>&1' >&2 || die "Zabbix の登録に失敗しました"
  ok "Zabbix の登録が終わりました"
}

# ---------------------------------------------------------------- タスクの中の起動スクリプト
# Valkey と Sentinel: k8s/valkey/start-*.sh と同じ設定。名前は Cloud Map の DNS（ME と NS はタスク定義の環境変数）
valkey_script() {
cat <<'EOF'
set -eu
FIRST="valkey-0.$NS"
PRIMARY=""
for i in 1 2 3; do
  for s in 0 1 2; do
    ans=$(valkey-cli -h "sentinel-$s.$NS" -p 26379 -a "$SENTINEL_PASSWORD" --no-auth-warning \
            SENTINEL get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
    if [ -n "$ans" ]; then PRIMARY="$ans"; break 2; fi
  done
  sleep 1
done
if [ -z "$PRIMARY" ]; then
  PRIMARY="$FIRST"
fi
{
  echo "port 6379"
  echo "dir /data"
  echo "appendonly yes"
  echo "appendfsync everysec"
  echo "save 900 1"
  echo "maxmemory 128mb"
  echo "maxmemory-policy noeviction"
  echo "min-replicas-to-write 1"
  echo "min-replicas-max-lag 10"
  echo "requirepass $REDIS_PASSWORD"
  echo "masterauth $REDIS_PASSWORD"
  echo "replica-announce-ip $ME"
  if [ "$PRIMARY" != "$ME" ]; then echo "replicaof $PRIMARY 6379"; fi
} > /tmp/valkey.conf
echo "start as $( [ "$PRIMARY" = "$ME" ] && echo primary || echo "replica of $PRIMARY" )"
exec valkey-server /tmp/valkey.conf
EOF
}
sentinel_script() {
cat <<'EOF'
set -eu
CONF=/conf/sentinel.conf
if [ ! -f "$CONF" ]; then
  PRIMARY=""
  for s in 0 1 2; do
    host="sentinel-$s.$NS"
    [ "$host" = "$ME" ] && continue
    ans=$(valkey-cli -h "$host" -p 26379 -a "$SENTINEL_PASSWORD" --no-auth-warning \
            SENTINEL get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
    if [ -n "$ans" ]; then PRIMARY="$ans"; break; fi
  done
  [ -n "$PRIMARY" ] || PRIMARY="valkey-0.$NS"
  until getent hosts "$PRIMARY" >/dev/null 2>&1; do sleep 1; done
  cat > "$CONF" <<EOT
port 26379
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip $ME
requirepass $SENTINEL_PASSWORD
sentinel sentinel-pass $SENTINEL_PASSWORD
sentinel monitor mymaster $PRIMARY 6379 2
sentinel auth-pass mymaster $REDIS_PASSWORD
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 60000
sentinel parallel-syncs mymaster 1
EOT
  echo "new sentinel config, watching $PRIMARY"
fi
exec valkey-sentinel "$CONF"
EOF
}
pgbouncer_script() {  # 今の Pooler と同じ設定（docs/design.md 7.3）
cat <<'EOF'
set -eu
umask 077
printf '"litellm" "%s"\n' "$DATABASE_PASSWORD" > /tmp/userlist.txt
cat > /tmp/pgbouncer.ini <<EOT
[databases]
litellm = host=$RDS_HOST port=5432 dbname=litellm
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = /tmp/userlist.txt
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 40
max_prepared_statements = 0
server_login_retry = 1
server_connect_timeout = 3
server_tls_sslmode = require
ignore_startup_parameters = extra_float_digits,options
EOT
exec pgbouncer /tmp/pgbouncer.ini
EOF
}
# 監視の台の初期化（ユーザーデータ。CloudFormation の Fn::Sub を通るので、${名前} はテンプレートの値、シェルの変数は $名前 だけで書く）
monitor_init() {
cat <<'EOF'
#!/bin/bash
set -uo pipefail
exec > >(tee -a /var/log/lab-monitor-init.log) 2>&1
RELAY="${Relay}"; LOCAL_ZBX="${LocalZbx}"; ZBX_SERVER="${ZbxServer}"; PORT="${ProxyPort}"
REGION="${AWS::Region}"; BUCKET="${ConfigBucket}"; PREFIX="${Prefix}"; EGRESS="${EgressProxy}"; NOPROXY="${NoProxy}"
WEB_PORT="${ZabbixWebPort}"
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/lab /opt/lab-zabbix
retry() { n=0; until "$@"; do n=$((n+1)); [ "$n" -ge 30 ] && return 1; sleep 10; done; }
s3get() {  # キー 保存先: インスタンスのロールで設定のバケットから読む（aws CLI を入れずに curl の SigV4 で）
  tok=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
  role=$(curl -s -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
  creds=$(curl -s -H "X-aws-ec2-metadata-token: $tok" "http://169.254.169.254/latest/meta-data/iam/security-credentials/$role")
  ak=$(printf '%s' "$creds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])')
  sk=$(printf '%s' "$creds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretAccessKey"])')
  st=$(printf '%s' "$creds" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Token"])')
  curl -sf --noproxy '*' --aws-sigv4 "aws:amz:$REGION:s3" --user "$ak:$sk" -H "x-amz-security-token: $st" \
    -H 'x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' \
    -o "$2" "https://$BUCKET.s3.$REGION.amazonaws.com/$1"
}
# 設定のバケットから読む道具（up のたびに SSM で流す監視の準備が使う）
cat > /usr/local/bin/lab-s3get <<'EOT'
#!/bin/bash
# 使い方: lab-s3get <キー> <保存先>。インスタンスのロールで設定のバケットから読む
set -euo pipefail
. /etc/lab/monitor.env
tok=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
role=$(curl -s -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
creds=$(curl -s -H "X-aws-ec2-metadata-token: $tok" "http://169.254.169.254/latest/meta-data/iam/security-credentials/$role")
get() { printf '%s' "$creds" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
curl -sf --noproxy '*' --aws-sigv4 "aws:amz:$REGION:s3" --user "$(get AccessKeyId):$(get SecretAccessKey)" \
  -H "x-amz-security-token: $(get Token)" -H 'x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' \
  -o "$2" "https://$BUCKET.s3.$REGION.amazonaws.com/$1"
EOT
chmod 755 /usr/local/bin/lab-s3get
mkdir -p /etc/lab
printf 'REGION=%s\nBUCKET=%s\nPREFIX=%s\nRELAY=%s\nEGRESS=%s\nNOPROXY=%s\n' "$REGION" "$BUCKET" "$PREFIX" "$RELAY" "$EGRESS" "$NOPROXY" > /etc/lab/monitor.env
# ---- 外向き: 検証（RELAY=true）は自分が中継なので直接。本番は社内プロキシと CA
if [ "$RELAY" != true ] && [ -n "$EGRESS" ]; then
  printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$EGRESS" "$EGRESS" > /etc/apt/apt.conf.d/95lab-proxy
  if retry s3get relay/egress-ca.pem /usr/local/share/ca-certificates/lab-egress.crt; then update-ca-certificates; fi
  export HTTP_PROXY="$EGRESS" HTTPS_PROXY="$EGRESS" http_proxy="$EGRESS" https_proxy="$EGRESS" NO_PROXY="$NOPROXY" no_proxy="$NOPROXY"
fi
retry apt-get update -q
retry apt-get install -y -q docker.io docker-compose-v2 python3-venv
if [ "$RELAY" != true ] && [ -n "$EGRESS" ]; then
  mkdir -p /etc/systemd/system/docker.service.d
  printf '[Service]\nEnvironment="HTTP_PROXY=%s" "HTTPS_PROXY=%s" "NO_PROXY=%s"\n' "$EGRESS" "$EGRESS" "$NOPROXY" \
    > /etc/systemd/system/docker.service.d/proxy.conf
  systemctl daemon-reload
fi
systemctl enable --now docker
systemctl restart docker
# ---- 検証の中継（mitmproxy。明示プロキシ、独自 CA。vm/proxy-init.sh と同じ）
if [ "$RELAY" = true ]; then
  if [ ! -x /opt/mitmproxy/bin/mitmdump ]; then
    python3 -m venv /opt/mitmproxy
    retry /opt/mitmproxy/bin/pip install -q --upgrade pip
    retry /opt/mitmproxy/bin/pip install -q "mitmproxy>=11"
  fi
  mkdir -p /etc/mitmproxy /var/log/lab-proxy
  retry s3get relay/mitmproxy-ca.pem /etc/mitmproxy/mitmproxy-ca.pem
  chmod 600 /etc/mitmproxy/mitmproxy-ca.pem
  cat > /etc/systemd/system/lab-proxy.service <<EOT
[Unit]
Description=litellm-lab: egress proxy (mitmproxy, explicit mode, private CA)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/opt/mitmproxy/bin/mitmdump --mode regular --listen-host 0.0.0.0 --listen-port $PORT --set block_global=false --set confdir=/etc/mitmproxy --set termlog_verbosity=warn -w /var/log/lab-proxy/flows
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOT
  systemctl daemon-reload
  systemctl enable --now lab-proxy.service
fi
# ---- Zabbix（proxy は本番も。server・web・DB は検証だけ）
if [ ! -f /opt/lab-zabbix/.env ]; then
  echo "POSTGRES_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" > /opt/lab-zabbix/.env
  chmod 600 /opt/lab-zabbix/.env
fi
{
  echo "services:"
  if [ "$LOCAL_ZBX" = true ]; then
    cat <<EOT
  db:
    image: postgres:16-alpine
    environment: {POSTGRES_USER: zabbix, POSTGRES_PASSWORD: "\$POSTGRES_PASSWORD", POSTGRES_DB: zabbix}
    volumes: ["db:/var/lib/postgresql/data"]
    restart: unless-stopped
  server:
    image: zabbix/zabbix-server-pgsql:ubuntu-7.0-latest
    environment: {DB_SERVER_HOST: db, POSTGRES_USER: zabbix, POSTGRES_PASSWORD: "\$POSTGRES_PASSWORD", POSTGRES_DB: zabbix, ZBX_CACHESIZE: 64M}
    ports: ["10051:10051"]
    depends_on: [db]
    restart: unless-stopped
  web:
    image: zabbix/zabbix-web-nginx-pgsql:ubuntu-7.0-latest
    environment: {DB_SERVER_HOST: db, POSTGRES_USER: zabbix, POSTGRES_PASSWORD: "\$POSTGRES_PASSWORD", POSTGRES_DB: zabbix, ZBX_SERVER_HOST: server, PHP_TZ: Asia/Tokyo}
    ports: ["$WEB_PORT:8080"]
    depends_on: [server]
    restart: unless-stopped
EOT
  fi
  cat <<EOT
  proxy:
    image: zabbix/zabbix-proxy-sqlite3:ubuntu-7.0-latest
    network_mode: host
    environment: {ZBX_PROXYMODE: "0", ZBX_HOSTNAME: "$PREFIX-proxy", ZBX_SERVER_HOST: "$ZBX_SERVER", ZBX_LISTENPORT: "10061", ZBX_PROXYCONFIGFREQUENCY: "60", ZBX_STARTHTTPPOLLERS: "5"}
    restart: unless-stopped
EOT
  if [ "$LOCAL_ZBX" = true ]; then printf 'volumes:\n  db: {}\n'; fi
} > /opt/lab-zabbix/compose.yaml
(cd /opt/lab-zabbix && retry docker compose up -d --quiet-pull)
# ---- Zabbix agent 2（この台自身と、ECS・Valkey・証明書を読む補助。設定は up のたびに SSM で流す準備が書く）
if ! command -v zabbix_agent2 >/dev/null 2>&1; then
  retry curl -sfL -o /tmp/zabbix-release.deb https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix-release/zabbix-release_latest_7.0+ubuntu24.04_all.deb
  dpkg -i /tmp/zabbix-release.deb && rm -f /tmp/zabbix-release.deb
  retry apt-get update -q
  retry apt-get install -y -q zabbix-agent2
fi
systemctl enable zabbix-agent2
touch /var/lib/lab/ready
echo "monitor ready"
EOF
}

# ---------------------------------------------------------------- テンプレート（環境ごとに bash が組み立てる）
subnets_list() { if [ "$INFRA" = managed ]; then echo '[!Ref Private0, !Ref Private1, !Ref Private2]'; else echo '!Ref SubnetIds'; fi; }
subnet_n() { if [ "$INFRA" = managed ]; then echo "!Ref Private$1"; else echo "!Select [$1, !Ref SubnetIds]"; fi; }
vpc_ref() { if [ "$INFRA" = managed ]; then echo '!Ref Vpc'; else echo '!Ref VpcId'; fi; }
keep_policy() { if [ "$SCALE" = prod ]; then echo "$1"; else echo Delete; fi; }
proxy_env() {  # 社内プロキシの環境変数（中継があるときだけ）。引数: インデント
  [ -n "$(egress_url)" ] || return 0
  local n
  for n in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do echo "$1- {Name: $n, Value: !Ref EgressProxy}"; done
  echo "$1- {Name: NO_PROXY, Value: !Ref NoProxy}"
  echo "$1- {Name: no_proxy, Value: !Ref NoProxy}"
}
ca_env() {  # CA の束。引数: インデント パス
  local n
  for n in SSL_CERT_FILE REQUESTS_CA_BUNDLE AWS_CA_BUNDLE CURL_CA_BUNDLE; do echo "$1- {Name: $n, Value: $2}"; done
}
egress_url() { if [ "$INFRA" = managed ]; then printf 'http://relay.%s:3128' "$NS"; else printf '%s' "${LAB_EGRESS_PROXY:-}"; fi; }
log_ref() { printf '!Ref Log%s' "$(printf '%s' "$1" | tr -cd 'a-zA-Z0-9')"; }
service_net() {  # 引数: インデント SG [サブネット]
  local subnets="${3:-$(subnets_list)}"
  cat <<EOF
$1NetworkConfiguration:
$1  AwsvpcConfiguration:
$1    Subnets: $subnets
$1    SecurityGroups: [!Ref $2]
$1    AssignPublicIp: DISABLED
EOF
}
exec_flag() { if is_true "${LAB_ECS_EXEC:-true}"; then echo true; else echo false; fi; }
task_head() {  # 論理 ID family cpu memory role
  cat <<EOF
  $1:
    Type: AWS::ECS::TaskDefinition
    Properties:
      Family: !Sub "\${Prefix}-$2"
      RequiresCompatibilities: [FARGATE]
      NetworkMode: awsvpc
      Cpu: "$3"
      Memory: "$4"
      RuntimePlatform: {CpuArchitecture: ARM64, OperatingSystemFamily: LINUX}
      ExecutionRoleArn: !GetAtt ExecRole.Arn
      TaskRoleArn: !GetAtt $5.Arn
EOF
}
logcfg() {  # グループ 接頭辞
  cat <<EOF
          LogConfiguration:
            LogDriver: awslogs
            Options: {awslogs-group: $(log_ref "$1"), awslogs-region: !Ref AWS::Region, awslogs-stream-prefix: $2}
EOF
}
deny_locked() {
  cat <<'EOF'
              - Effect: Deny
                Action: [s3:DeleteObject, s3:DeleteObjectVersion, s3:BypassGovernanceRetention, s3:PutObjectRetention,
                         s3:PutObjectLegalHold, s3:PutBucketObjectLockConfiguration]
                Resource:
                  - !Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-audit-raw"
                  - !Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-audit-raw/*"
                  - !Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-audit-access"
                  - !Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-audit-access/*"
              - Effect: Allow
                Action: [ssmmessages:CreateControlChannel, ssmmessages:CreateDataChannel, ssmmessages:OpenControlChannel, ssmmessages:OpenDataChannel]
                Resource: "*"
              - {Effect: Allow, Action: [elasticfilesystem:ClientMount, elasticfilesystem:ClientWrite], Resource: !GetAtt Efs.Arn}
EOF
}
task_role() {  # 論理 ID と、その役割の S3 の許可（render.s3_policies と同じ。行を標準入力で）
  cat <<EOF
  $1:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement: [{Effect: Allow, Principal: {Service: ecs-tasks.amazonaws.com}, Action: sts:AssumeRole,
                     Condition: {StringEquals: {"aws:SourceAccount": !Ref AWS::AccountId}}}]
      Policies:
        - PolicyName: lab
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
EOF
  cat
  deny_locked
}
bucket_arn() { printf '!Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-%s"' "$1"; }

render_template() {
  local s3d="$(keep_policy Retain)"
cat <<'EOF'
AWSTemplateFormatVersion: "2010-09-09"
Description: litellm-agent-gateway-lab ECS configuration (LAB_TARGET=aws-ecs). Generated by ecs/ecs-lab.sh

Parameters:
  Prefix: {Type: String}
  VpcCidr: {Type: String}
  VpcId: {Type: String, Default: ""}
  SubnetIds: {Type: CommaDelimitedList, Default: ""}
  OperatorCidr: {Type: String}
  BastionCidr: {Type: String, Default: ""}
  ClientCidr: {Type: String, Default: ""}
  CertificateArn: {Type: String}
  BaseUrl: {Type: String}
  ImageLitellm: {Type: String}
  ImageMigrate: {Type: String}
  ImageAudit: {Type: String}
  ImageFakeLlm: {Type: String}
  ImageValkey: {Type: String}
  ImagePgbouncer: {Type: String}
  ImageMcp: {Type: String}
  ConfigBucket: {Type: String}
  LitellmConfigKey: {Type: String}
  EnvLitellm: {Type: String}
  EnvPgbouncer: {Type: String}
  EnvValkey: {Type: String}
  EnvViewer: {Type: String}
  EnvMcp: {Type: String}
  LitellmEnabled: {Type: String, AllowedValues: ["true", "false"]}
  LitellmCpu: {Type: Number}
  LitellmMemory: {Type: Number}
  LitellmMin: {Type: Number}
  LitellmMax: {Type: Number}
  RdsClass: {Type: String}
  RdsStorage: {Type: Number}
  PgVersion: {Type: String}
  PostgresPassword: {Type: String, NoEcho: true}
  PoolerMode: {Type: String, AllowedValues: [pooler, direct]}
  EgressProxy: {Type: String, Default: ""}
  NoProxy: {Type: String}
  LockMode: {Type: String, AllowedValues: [GOVERNANCE, COMPLIANCE]}
  RetentionDays: {Type: Number}
  AccessRetentionDays: {Type: Number}
  RetentionExpireDays: {Type: Number}
  AccessExpireDays: {Type: Number}
  ZabbixServer: {Type: String, Default: ""}
  ZabbixWebPort: {Type: Number}
  MonitorType: {Type: String}
  MonitorAmi:
    Type: AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>
    Default: /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id
  HasGoogle: {Type: String, AllowedValues: ["true", "false"]}
  FakeLlm: {Type: String, AllowedValues: ["true", "false"]}
  AuditAdmins: {Type: String, Default: ""}
  AuditAuditors: {Type: String, Default: ""}

Conditions:
  Pooler: !Equals [!Ref PoolerMode, pooler]
  LocalZabbix: !Equals [!Ref ZabbixServer, ""]
  LitellmOn: !Equals [!Ref LitellmEnabled, "true"]
  GoogleOn: !Equals [!Ref HasGoogle, "true"]
  FakeOn: !Equals [!Ref FakeLlm, "true"]
  HasClientCidr: !Not [!Equals [!Ref ClientCidr, ""]]

Resources:
EOF
  # ---------------- ネットワーク（managed だけ。private のサブネットにはインターネットへの経路を作らない）
  if [ "$INFRA" = managed ]; then
cat <<'EOF'
  Vpc:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: !Ref VpcCidr
      EnableDnsHostnames: true
      EnableDnsSupport: true
      Tags: [{Key: Name, Value: !Ref Prefix}]
EOF
    local i
    for i in 0 1 2; do
cat <<EOF
  Private$i:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      AvailabilityZone: !Select [$i, !GetAZs ""]
      CidrBlock: !Select [$i, !Cidr [!Ref VpcCidr, 8, 12]]
      Tags: [{Key: Name, Value: !Sub "\${Prefix}-private-$i"}]
  Private${i}Routes:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties: {SubnetId: !Ref Private$i, RouteTableId: !Ref PrivateRoutes}
EOF
    done
cat <<'EOF'
  PrivateRoutes:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref Vpc
      Tags: [{Key: Name, Value: !Sub "${Prefix}-private"}]
  # 検証の中継（監視の台）だけを置くサブネット。インターネットゲートウェイへの経路がある
  RelaySubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      AvailabilityZone: !Select [0, !GetAZs ""]
      CidrBlock: !Select [255, !Cidr [!Ref VpcCidr, 256, 8]]
      Tags: [{Key: Name, Value: !Sub "${Prefix}-relay"}]
  Igw:
    Type: AWS::EC2::InternetGateway
  IgwAttach:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties: {VpcId: !Ref Vpc, InternetGatewayId: !Ref Igw}
  RelayRoutes:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref Vpc
      Tags: [{Key: Name, Value: !Sub "${Prefix}-relay"}]
  RelayDefault:
    Type: AWS::EC2::Route
    DependsOn: IgwAttach
    Properties: {RouteTableId: !Ref RelayRoutes, DestinationCidrBlock: 0.0.0.0/0, GatewayId: !Ref Igw}
  RelaySubnetRoutes:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties: {SubnetId: !Ref RelaySubnet, RouteTableId: !Ref RelayRoutes}
  S3Endpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcId: !Ref Vpc
      ServiceName: !Sub "com.amazonaws.${AWS::Region}.s3"
      VpcEndpointType: Gateway
      RouteTableIds: [!Ref PrivateRoutes, !Ref RelayRoutes]
  EndpointSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: VPC interface endpoints
      VpcId: !Ref Vpc
      SecurityGroupIngress: [{IpProtocol: tcp, FromPort: 443, ToPort: 443, CidrIp: !Ref VpcCidr}]
EOF
    local svc
    for svc in ecr.api ecr.dkr logs ssm ssmmessages ec2messages monitoring ecs; do
cat <<EOF
  Endpoint$(printf '%s' "$svc" | tr -cd 'a-z0-9'):
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcId: !Ref Vpc
      ServiceName: !Sub "com.amazonaws.\${AWS::Region}.$svc"
      VpcEndpointType: Interface
      PrivateDnsEnabled: true
      SubnetIds: [!Ref Private0, !Ref Private1, !Ref Private2]
      SecurityGroupIds: [!Ref EndpointSg]
EOF
    done
  fi
  # ---------------- セキュリティグループ
  local v; v=$(vpc_ref)
cat <<EOF
  AlbSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: internal ALB
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 443, ToPort: 443, CidrIp: !Ref VpcCidr}
        - {IpProtocol: tcp, FromPort: 80, ToPort: 80, CidrIp: !Ref VpcCidr}
        - !If [HasClientCidr, {IpProtocol: tcp, FromPort: 443, ToPort: 443, CidrIp: !Ref ClientCidr}, !Ref AWS::NoValue]
  LitellmSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: LiteLLM tasks
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 4000, ToPort: 4000, SourceSecurityGroupId: !Ref AlbSg}
        - {IpProtocol: tcp, FromPort: 4000, ToPort: 4000, SourceSecurityGroupId: !Ref MonitorSg}
  MigrateSg:
    Type: AWS::EC2::SecurityGroup
    Properties: {GroupDescription: DB migration task, VpcId: $v}
  PgbouncerSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: PgBouncer tasks
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 5432, ToPort: 5432, SourceSecurityGroupId: !Ref LitellmSg}
  RdsSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: RDS for PostgreSQL
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 5432, ToPort: 5432, SourceSecurityGroupId: !Ref PgbouncerSg}
        - {IpProtocol: tcp, FromPort: 5432, ToPort: 5432, SourceSecurityGroupId: !Ref LitellmSg}
        - {IpProtocol: tcp, FromPort: 5432, ToPort: 5432, SourceSecurityGroupId: !Ref MigrateSg}
        - {IpProtocol: tcp, FromPort: 5432, ToPort: 5432, SourceSecurityGroupId: !Ref MonitorSg}
  RedisSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Valkey and Sentinel tasks
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 6379, ToPort: 6379, SourceSecurityGroupId: !Ref LitellmSg}
        - {IpProtocol: tcp, FromPort: 26379, ToPort: 26379, SourceSecurityGroupId: !Ref LitellmSg}
        - {IpProtocol: tcp, FromPort: 6379, ToPort: 6379, SourceSecurityGroupId: !Ref MonitorSg}
        - {IpProtocol: tcp, FromPort: 26379, ToPort: 26379, SourceSecurityGroupId: !Ref MonitorSg}
  RedisSelf6379:
    Type: AWS::EC2::SecurityGroupIngress
    Properties: {GroupId: !Ref RedisSg, IpProtocol: tcp, FromPort: 6379, ToPort: 6379, SourceSecurityGroupId: !Ref RedisSg}
  RedisSelf26379:
    Type: AWS::EC2::SecurityGroupIngress
    Properties: {GroupId: !Ref RedisSg, IpProtocol: tcp, FromPort: 26379, ToPort: 26379, SourceSecurityGroupId: !Ref RedisSg}
  ViewerSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: audit viewer tasks
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 8000, ToPort: 8000, SourceSecurityGroupId: !Ref AlbSg}
  InternalSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: workspace-mcp, fake-llm, compactor (called only by LiteLLM)
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 8000, ToPort: 8000, SourceSecurityGroupId: !Ref LitellmSg}
  EfsSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: EFS mount targets
      VpcId: $v
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 2049, ToPort: 2049, SourceSecurityGroupId: !Ref RedisSg}
  MonitorSg:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: monitor host (Zabbix proxy; relay and bastion in lab)
      VpcId: $v
EOF
  if [ "$INFRA" = managed ]; then
cat <<'EOF'
      SecurityGroupIngress:
        - {IpProtocol: tcp, FromPort: 3128, ToPort: 3128, CidrIp: !Ref VpcCidr}
        - {IpProtocol: tcp, FromPort: 3128, ToPort: 3128, CidrIp: !Ref OperatorCidr}
        # 検証の Zabbix サーバー（10051）と web の API（8080）は VPC の中から（同じ VPC に置く provided の試験が、既存のサーバーとして使う）
        - {IpProtocol: tcp, FromPort: 10051, ToPort: 10051, CidrIp: !Ref VpcCidr}
        - {IpProtocol: tcp, FromPort: 8080, ToPort: 8080, CidrIp: !Ref VpcCidr}
EOF
  fi
  # ---------------- 入口（内部 ALB）
  local admin_src
  if [ "$INFRA" = managed ]; then admin_src='!Sub "${Monitor.PrivateIp}/32"'; else admin_src='!Ref BastionCidr'; fi
cat <<EOF
  Alb:
    Type: AWS::ElasticLoadBalancingV2::LoadBalancer
    Properties:
      Name: !Sub "\${Prefix}-alb"
      Scheme: internal
      Type: application
      Subnets: $(subnets_list)
      SecurityGroups: [!Ref AlbSg]
      LoadBalancerAttributes:
        - {Key: idle_timeout.timeout_seconds, Value: "1800"}
        - {Key: deletion_protection.enabled, Value: "$( [ "$SCALE" = prod ] && echo true || echo false)"}
        - {Key: routing.http.drop_invalid_header_fields.enabled, Value: "true"}
  LitellmTg:
    Type: AWS::ElasticLoadBalancingV2::TargetGroup
    Properties:
      VpcId: $v
      TargetType: ip
      Protocol: HTTP
      Port: 4000
      HealthCheckPath: /health/readiness
      HealthCheckIntervalSeconds: 10
      HealthCheckTimeoutSeconds: 5
      HealthyThresholdCount: 2
      UnhealthyThresholdCount: 2
      TargetGroupAttributes: [{Key: deregistration_delay.timeout_seconds, Value: "60"}]
  ViewerTg:
    Type: AWS::ElasticLoadBalancingV2::TargetGroup
    Properties:
      VpcId: $v
      TargetType: ip
      Protocol: HTTP
      Port: 8000
      HealthCheckPath: /lab-audit/healthz
      HealthCheckIntervalSeconds: 10
      HealthyThresholdCount: 2
      UnhealthyThresholdCount: 2
      TargetGroupAttributes: [{Key: deregistration_delay.timeout_seconds, Value: "30"}]
  Http:
    Type: AWS::ElasticLoadBalancingV2::Listener
    Properties:
      LoadBalancerArn: !Ref Alb
      Port: 80
      Protocol: HTTP
      DefaultActions: [{Type: redirect, RedirectConfig: {Protocol: HTTPS, Port: "443", StatusCode: HTTP_301}}]
  Https:
    Type: AWS::ElasticLoadBalancingV2::Listener
    Properties:
      LoadBalancerArn: !Ref Alb
      Port: 443
      Protocol: HTTPS
      SslPolicy: ELBSecurityPolicy-TLS13-1-2-2021-06
      Certificates: [{CertificateArn: !Ref CertificateArn}]
      DefaultActions: [{Type: forward, TargetGroupArn: !Ref LitellmTg}]
  # 素通しの経路（spec 4.2）は 403。1 つの規則の条件の値は 5 つまでなので 2 つに分ける
  RulePassthroughA:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 1
      Conditions: [{Field: path-pattern, PathPatternConfig: {Values: ["/anthropic*", "/openai*", "/azure*", "/bedrock*", "/vertex_ai*"]}}]
      Actions: [{Type: fixed-response, FixedResponseConfig: {StatusCode: "403", ContentType: text/plain, MessageBody: forbidden}}]
  RulePassthroughB:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 2
      Conditions: [{Field: path-pattern, PathPatternConfig: {Values: ["/gemini*", "/cohere*", "/mistral*", "/assemblyai*", "/langfuse*"]}}]
      Actions: [{Type: fixed-response, FixedResponseConfig: {StatusCode: "403", ContentType: text/plain, MessageBody: forbidden}}]
  # MCP の経路（/<サーバー名>/mcp）への GET は 405（k8s/ingress.yaml と同じ。Codex が待ち続けないように）
  RuleMcpGet:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 3
      Conditions:
        - {Field: http-request-method, HttpRequestMethodConfig: {Values: [GET]}}
        - {Field: path-pattern, PathPatternConfig: {Values: ["/*/mcp", "/*/mcp/"]}}
      Actions: [{Type: fixed-response, FixedResponseConfig: {StatusCode: "405", ContentType: text/plain, MessageBody: method not allowed}}]
  # 管理系の経路（render.ADMIN_PATH_PREFIXES）と検索アプリは踏み台からだけ
  RuleAdminAllow:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 10
      Conditions:
        - {Field: path-pattern, PathPatternConfig: {Values: ["/ui*", "/login*", "/sso*", "/metrics*"]}}
        - {Field: source-ip, SourceIpConfig: {Values: [$admin_src]}}
      Actions: [{Type: forward, TargetGroupArn: !Ref LitellmTg}]
  RuleAdminDeny:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 11
      Conditions: [{Field: path-pattern, PathPatternConfig: {Values: ["/ui*", "/login*", "/sso*", "/metrics*"]}}]
      Actions: [{Type: fixed-response, FixedResponseConfig: {StatusCode: "403", ContentType: text/plain, MessageBody: forbidden}}]
  RuleAuditAllow:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 20
      Conditions:
        - {Field: path-pattern, PathPatternConfig: {Values: ["/lab-audit", "/lab-audit/*"]}}
        - {Field: source-ip, SourceIpConfig: {Values: [$admin_src]}}
      Actions: [{Type: forward, TargetGroupArn: !Ref ViewerTg}]
  RuleAuditDeny:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Priority: 21
      Conditions: [{Field: path-pattern, PathPatternConfig: {Values: ["/lab-audit", "/lab-audit/*"]}}]
      Actions: [{Type: fixed-response, FixedResponseConfig: {StatusCode: "403", ContentType: text/plain, MessageBody: forbidden}}]

  # RDS for PostgreSQL（マルチ AZ の DB インスタンス。同期複製の standby 1 台）
  DbSubnets:
    Type: AWS::RDS::DBSubnetGroup
    Properties:
      DBSubnetGroupDescription: !Sub "\${Prefix} private subnets"
      SubnetIds: $(subnets_list)
  DbParams:
    Type: AWS::RDS::DBParameterGroup
    Properties:
      Family: postgres17
      Description: !Sub "\${Prefix} postgres17"
      Parameters: {max_connections: "200", rds.force_ssl: "1"}
  Db:
    Type: AWS::RDS::DBInstance
    DeletionPolicy: $(keep_policy Snapshot)
    UpdateReplacePolicy: $(keep_policy Snapshot)
    Properties:
      DBInstanceIdentifier: !Sub "\${Prefix}-pg"
      Engine: postgres
      EngineVersion: !Ref PgVersion
      DBInstanceClass: !Ref RdsClass
      AllocatedStorage: !Ref RdsStorage
      StorageType: gp3
      StorageEncrypted: true
      MultiAZ: true
      DBName: litellm
      MasterUsername: litellm
      MasterUserPassword: !Ref PostgresPassword
      DBSubnetGroupName: !Ref DbSubnets
      DBParameterGroupName: !Ref DbParams
      VPCSecurityGroups: [!Ref RdsSg]
      BackupRetentionPeriod: 30
      DeletionProtection: $( [ "$SCALE" = prod ] && echo true || echo false)
      CopyTagsToSnapshot: true
      AutoMinorVersionUpgrade: true
      PubliclyAccessible: false

  # EFS（Valkey の AOF と Sentinel が書き換える設定）
  Efs:
    Type: AWS::EFS::FileSystem
    DeletionPolicy: $(keep_policy Retain)
    UpdateReplacePolicy: $(keep_policy Retain)
    Properties:
      Encrypted: true
      FileSystemTags: [{Key: Name, Value: !Ref Prefix}]
EOF
  for i in 0 1 2; do
cat <<EOF
  EfsMount$i:
    Type: AWS::EFS::MountTarget
    Properties: {FileSystemId: !Ref Efs, SubnetId: $(subnet_n "$i"), SecurityGroups: [!Ref EfsSg]}
EOF
    for n in valkey sentinel; do
cat <<EOF
  Ap$n$i:
    Type: AWS::EFS::AccessPoint
    Properties:
      FileSystemId: !Ref Efs
      PosixUser: {Uid: "999", Gid: "999"}
      RootDirectory: {Path: /$n-$i, CreationInfo: {OwnerUid: "999", OwnerGid: "999", Permissions: "750"}}
EOF
    done
  done
  # ---------------- S3（render.s3_buckets と同じ定義。ロック付きは CloudFormation が空にできないので残し、down が消す）
  local name lock days expire
  for name in raw access; do
    if [ $name = raw ]; then days=RetentionDays; expire=RetentionExpireDays; else days=AccessRetentionDays; expire=AccessExpireDays; fi
cat <<EOF
  $(printf '%s' "$name" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')Bucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      BucketName: !Sub "\${Prefix}-\${AWS::AccountId}-audit-$name"
      ObjectLockEnabled: true
      ObjectLockConfiguration: {ObjectLockEnabled: Enabled, Rule: {DefaultRetention: {Mode: !Ref LockMode, Days: !Ref $days}}}
      VersioningConfiguration: {Status: Enabled}
      BucketEncryption: {ServerSideEncryptionConfiguration: [{ServerSideEncryptionByDefault: {SSEAlgorithm: AES256}}]}
      PublicAccessBlockConfiguration: {BlockPublicAcls: true, IgnorePublicAcls: true, BlockPublicPolicy: true, RestrictPublicBuckets: true}
      LifecycleConfiguration:
        Rules:
          - {Id: all, Status: Enabled, Prefix: "", ExpirationInDays: !Ref $expire, NoncurrentVersionExpiration: {NoncurrentDays: !Ref $expire}}
EOF
  done
cat <<'EOF'
  DerivedBucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      BucketName: !Sub "${Prefix}-${AWS::AccountId}-audit-derived"
      VersioningConfiguration: {Status: Enabled}
      BucketEncryption: {ServerSideEncryptionConfiguration: [{ServerSideEncryptionByDefault: {SSEAlgorithm: AES256}}]}
      PublicAccessBlockConfiguration: {BlockPublicAcls: true, IgnorePublicAcls: true, BlockPublicPolicy: true, RestrictPublicBuckets: true}
      LifecycleConfiguration:
        Rules:
          - {Id: meta, Status: Enabled, Prefix: meta/, ExpirationInDays: !Ref RetentionExpireDays, NoncurrentVersionExpiration: {NoncurrentDays: 1}}
          - {Id: meta-day, Status: Enabled, Prefix: meta_day/, ExpirationInDays: !Ref RetentionExpireDays, NoncurrentVersionExpiration: {NoncurrentDays: 1}}
          - {Id: usage-daily, Status: Enabled, Prefix: usage_daily/, ExpirationInDays: 400, NoncurrentVersionExpiration: {NoncurrentDays: 1}}
          - {Id: settlement, Status: Enabled, Prefix: billing/settlement/, ExpirationInDays: 400, NoncurrentVersionExpiration: {NoncurrentDays: 400}}
          - {Id: synth, Status: Enabled, Prefix: synth/, ExpirationInDays: 7, NoncurrentVersionExpiration: {NoncurrentDays: 1}}
EOF
  for name in RawBucket AccessBucket DerivedBucket; do
cat <<EOF
  ${name}Policy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref $name
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: TlsOnly
            Effect: Deny
            Principal: "*"
            Action: "s3:*"
            Resource: [!GetAtt $name.Arn, !Sub "\${$name.Arn}/*"]
            Condition: {Bool: {"aws:SecureTransport": "false"}}
EOF
  done
  # ---------------- IAM（S3 の許可は render.s3_policies と同じ）
cat <<'EOF'
  ExecRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement: [{Effect: Allow, Principal: {Service: ecs-tasks.amazonaws.com}, Action: sts:AssumeRole,
                     Condition: {StringEquals: {"aws:SourceAccount": !Ref AWS::AccountId}}}]
      ManagedPolicyArns: [arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy]
      Policies:
        - PolicyName: env-files
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - {Effect: Allow, Action: s3:GetObject, Resource: !Sub "arn:aws:s3:::${ConfigBucket}/env/*"}
              - {Effect: Allow, Action: s3:GetBucketLocation, Resource: !Sub "arn:aws:s3:::${ConfigBucket}"}
EOF
  task_role LitellmRole <<EOF
              - {Effect: Allow, Action: s3:PutObject, Resource: $(bucket_arn audit-raw/raw/*)}
              - {Effect: Allow, Action: s3:GetObject, Resource: !Sub "arn:aws:s3:::\${ConfigBucket}/litellm/*"}
EOF
  task_role ViewerRole <<EOF
              - {Effect: Allow, Action: s3:ListBucket, Resource: [$(bucket_arn audit-raw), $(bucket_arn audit-access), $(bucket_arn audit-derived)]}
              - {Effect: Allow, Action: s3:GetObject, Resource: [$(bucket_arn 'audit-raw/*'), $(bucket_arn 'audit-access/*'), $(bucket_arn 'audit-derived/*')]}
              - {Effect: Allow, Action: s3:PutObject, Resource: [$(bucket_arn 'audit-access/*'), $(bucket_arn 'audit-derived/billing/contracts/*'), $(bucket_arn 'audit-derived/billing/fx_rates/*')]}
EOF
  task_role CompactorRole <<EOF
              - {Effect: Allow, Action: s3:ListBucket, Resource: [$(bucket_arn audit-raw), $(bucket_arn audit-derived)]}
              - {Effect: Allow, Action: s3:GetObject, Resource: [$(bucket_arn 'audit-raw/*'), $(bucket_arn 'audit-derived/*')]}
              - {Effect: Allow, Action: s3:PutObject, Resource: $(bucket_arn 'audit-derived/*')}
EOF
  task_role BasicRole < /dev/null
cat <<'EOF'
  MonitorRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement: [{Effect: Allow, Principal: {Service: ec2.amazonaws.com}, Action: sts:AssumeRole}]
      ManagedPolicyArns: [arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore]
      Policies:
        - PolicyName: read-only
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              # Zabbix の AWS 用のテンプレートと、LiteLLM のタスクの一覧（読み取りだけ）
              - Effect: Allow
                Action: [cloudwatch:GetMetricData, cloudwatch:ListMetrics, cloudwatch:DescribeAlarms, ecs:ListClusters, ecs:ListServices,
                         ecs:ListTasks, ecs:DescribeClusters, ecs:DescribeServices, ecs:DescribeTasks, elasticloadbalancing:Describe*,
                         rds:DescribeDBInstances, rds:DescribeDBClusters, rds:DescribeEvents, ec2:DescribeInstances, ec2:DescribeVolumes,
                         tag:GetResources]
                Resource: "*"
              - Effect: Allow
                Action: s3:GetObject
                Resource:
                  - !Sub "arn:aws:s3:::${ConfigBucket}/relay/*"
                  - !Sub "arn:aws:s3:::${ConfigBucket}/zabbix/*"
                  - !Sub "arn:aws:s3:::${Prefix}-${AWS::AccountId}-audit-derived/_state/*"
  MonitorProfile:
    Type: AWS::IAM::InstanceProfile
    Properties: {Roles: [!Ref MonitorRole]}
  SchedulerRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement: [{Effect: Allow, Principal: {Service: scheduler.amazonaws.com}, Action: sts:AssumeRole,
                     Condition: {StringEquals: {"aws:SourceAccount": !Ref AWS::AccountId}}}]
      Policies:
        - PolicyName: run-compactor
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - {Effect: Allow, Action: ecs:RunTask, Resource: !Sub "arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/${Prefix}-audit-compactor:*"}
              - {Effect: Allow, Action: iam:PassRole, Resource: [!GetAtt ExecRole.Arn, !GetAtt CompactorRole.Arn]}

  # ECS
  Namespace:
    Type: AWS::ServiceDiscovery::PrivateDnsNamespace
    Properties:
      Name: !Sub "${Prefix}.internal"
EOF
  echo "      Vpc: $v"
cat <<'EOF'
  Cluster:
    Type: AWS::ECS::Cluster
    Properties:
      ClusterName: !Ref Prefix
      ClusterSettings: [{Name: containerInsights, Value: enhanced}]
      CapacityProviders: [FARGATE]
      ServiceConnectDefaults: {Namespace: !GetAtt Namespace.Arn}
EOF
  for name in litellm litellm-migrate pgbouncer valkey sentinel audit-viewer audit-compactor workspace-mcp fake-llm service-connect; do
cat <<EOF
  Log$(printf '%s' "$name" | tr -cd 'a-zA-Z0-9'):
    Type: AWS::Logs::LogGroup
    Properties: {LogGroupName: !Sub "/\${Prefix}/$name", RetentionInDays: 30}
EOF
  done
  if [ "$INFRA" = managed ]; then
cat <<'EOF'
  RelayRecord:
    Type: AWS::ServiceDiscovery::Service
    Properties:
      Name: relay
      NamespaceId: !GetAtt Namespace.Id
      DnsConfig: {DnsRecords: [{Type: A, TTL: 60}], RoutingPolicy: MULTIVALUE}
  RelayInstance:
    Type: AWS::ServiceDiscovery::Instance
    Properties:
      ServiceId: !GetAtt RelayRecord.Id
      InstanceId: relay
      InstanceAttributes: {AWS_INSTANCE_IPV4: !GetAtt Monitor.PrivateIp}
EOF
  fi
  # ---------------- LiteLLM
  task_head LitellmTask litellm '${LitellmCpu}' '${LitellmMemory}' LitellmRole | sed 's/Cpu: "\${LitellmCpu}"/Cpu: !Ref LitellmCpu/; s/Memory: "\${LitellmMemory}"/Memory: !Ref LitellmMemory/'
cat <<EOF
      EphemeralStorage: {SizeInGiB: 21}
      ContainerDefinitions:
        - Name: litellm
          Image: !Ref ImageLitellm
          Essential: true
          Command: ["--port", "4000", "--num_workers", "1"]
          PortMappings: [{Name: http, ContainerPort: 4000, Protocol: tcp, AppProtocol: http}]
          StopTimeout: 120
          LinuxParameters: {InitProcessEnabled: true}
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvLitellm}"}]
          Environment:
            - {Name: LITELLM_MODE, Value: PRODUCTION}
            - {Name: PROXY_BASE_URL, Value: !Ref BaseUrl}
            - {Name: UI_USERNAME, Value: admin}
            - {Name: AUDIT_FLUSH_SECONDS, Value: "10"}
            - {Name: DISABLE_SCHEMA_UPDATE, Value: "true"}
            - {Name: LITELLM_CONFIG_BUCKET_NAME, Value: !Ref ConfigBucket}
            - {Name: LITELLM_CONFIG_BUCKET_OBJECT_KEY, Value: !Ref LitellmConfigKey}
            - {Name: DATABASE_HOST, Value: !If [Pooler, "pgbouncer:5432", !Sub "\${Db.Endpoint.Address}:5432"]}
            - {Name: DATABASE_USERNAME, Value: litellm}
            - {Name: DATABASE_NAME, Value: litellm}
            - !If [Pooler, {Name: DATABASE_DISABLE_PREPARED_STATEMENTS, Value: "true"}, {Name: DATABASE_SSLMODE, Value: require}]
            - {Name: REDIS_SENTINEL_NODES, Value: !Sub '[["sentinel-0.\${Prefix}.internal", 26379], ["sentinel-1.\${Prefix}.internal", 26379], ["sentinel-2.\${Prefix}.internal", 26379]]'}
            - {Name: REDIS_SERVICE_NAME, Value: mymaster}
            - {Name: AUDIT_S3_ENDPOINT, Value: ""}
            - {Name: AUDIT_S3_REGION, Value: !Ref AWS::Region}
            - {Name: AUDIT_S3_BUCKET, Value: !Ref RawBucket}
            - {Name: AUDIT_S3_CREDENTIAL_CHAIN, Value: "true"}
            - {Name: AWS_REGION, Value: !Ref AWS::Region}
            - {Name: AWS_DEFAULT_REGION, Value: !Ref AWS::Region}
$(proxy_env '            ')
$( [ -n "$(egress_url)" ] && echo '            - {Name: DISABLE_AIOHTTP_TRANSPORT, Value: "True"}')
$(ca_env '            ' /etc/lab/ca-bundle.pem)
          HealthCheck:
            Command: [CMD-SHELL, "python -c \\"import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=4)\\""]
            Interval: 15
            Timeout: 5
            Retries: 3
            StartPeriod: 90
$(logcfg litellm litellm)
  LitellmService:
    Type: AWS::ECS::Service
    DependsOn: [Https, PgbouncerService, Valkey0Service, Sentinel0Service, Sentinel1Service, Sentinel2Service]
    Properties:
      ServiceName: litellm
      Cluster: !Ref Cluster
      TaskDefinition: !Ref LitellmTask
      LaunchType: FARGATE
      DesiredCount: !If [LitellmOn, !Ref LitellmMin, 0]
      EnableExecuteCommand: $(exec_flag)
      HealthCheckGracePeriodSeconds: 120
      DeploymentConfiguration:
        MinimumHealthyPercent: 100
        MaximumPercent: 200
        DeploymentCircuitBreaker: {Enable: true, Rollback: true}
$(service_net '      ' LitellmSg)
      LoadBalancers: [{ContainerName: litellm, ContainerPort: 4000, TargetGroupArn: !Ref LitellmTg}]
      ServiceConnectConfiguration:
        Enabled: true
        Namespace: !GetAtt Namespace.Arn
        LogConfiguration:
          LogDriver: awslogs
          Options: {awslogs-group: $(log_ref service-connect), awslogs-region: !Ref AWS::Region, awslogs-stream-prefix: litellm}
  LitellmScaling:
    Type: AWS::ApplicationAutoScaling::ScalableTarget
    Condition: LitellmOn
    Properties:
      ServiceNamespace: ecs
      ScalableDimension: ecs:service:DesiredCount
      ResourceId: !Sub "service/\${Cluster}/\${LitellmService.Name}"
      MinCapacity: !Ref LitellmMin
      MaxCapacity: !Ref LitellmMax
  LitellmScalingPolicy:
    Type: AWS::ApplicationAutoScaling::ScalingPolicy
    Condition: LitellmOn
    Properties:
      PolicyName: cpu60
      PolicyType: TargetTrackingScaling
      ScalingTargetId: !Ref LitellmScaling
      TargetTrackingScalingPolicyConfiguration:
        TargetValue: 60
        ScaleInCooldown: 300
        ScaleOutCooldown: 60
        PredefinedMetricSpecification: {PredefinedMetricType: ECSServiceAverageCPUUtilization}
EOF
  # ---------------- DB 移行（単発。primary に直結。PgBouncer は通さない）
  task_head MigrateTask litellm-migrate 1024 4096 BasicRole
cat <<EOF
      ContainerDefinitions:
        - Name: migrate
          Image: !Ref ImageMigrate
          Essential: true
          EntryPoint: [python]
          Command: [litellm/proxy/prisma_migration.py]
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvLitellm}"}]
          Environment:
            - {Name: DATABASE_HOST, Value: !Sub "\${Db.Endpoint.Address}:5432"}
            - {Name: DATABASE_USERNAME, Value: litellm}
            - {Name: DATABASE_NAME, Value: litellm}
            - {Name: DATABASE_SSLMODE, Value: require}
            - {Name: DISABLE_SCHEMA_UPDATE, Value: "false"}
$(logcfg litellm-migrate migrate)
EOF
  # ---------------- PgBouncer
  task_head PgbouncerTask pgbouncer 256 512 BasicRole
cat <<EOF
      ContainerDefinitions:
        - Name: pgbouncer
          Image: !Ref ImagePgbouncer
          Essential: true
          EntryPoint: [sh, -c]
          Command:
            - |
$(pgbouncer_script | indent '              ')
          PortMappings: [{Name: pg, ContainerPort: 5432, Protocol: tcp}]
          LinuxParameters: {InitProcessEnabled: true}
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvPgbouncer}"}]
          Environment: [{Name: RDS_HOST, Value: !GetAtt Db.Endpoint.Address}]
$(logcfg pgbouncer pgbouncer)
  PgbouncerService:
    Type: AWS::ECS::Service
    Properties:
      ServiceName: pgbouncer
      Cluster: !Ref Cluster
      TaskDefinition: !Ref PgbouncerTask
      LaunchType: FARGATE
      DesiredCount: 2
      EnableExecuteCommand: $(exec_flag)
      DeploymentConfiguration:
        MinimumHealthyPercent: 100
        MaximumPercent: 200
        DeploymentCircuitBreaker: {Enable: true, Rollback: true}
$(service_net '      ' PgbouncerSg)
      ServiceConnectConfiguration:
        Enabled: true
        Namespace: !GetAtt Namespace.Arn
        Services: [{PortName: pg, DiscoveryName: pgbouncer, ClientAliases: [{Port: 5432, DnsName: pgbouncer}]}]
        LogConfiguration:
          LogDriver: awslogs
          Options: {awslogs-group: $(log_ref service-connect), awslogs-region: !Ref AWS::Region, awslogs-stream-prefix: pgbouncer}
EOF
  # ---------------- Valkey 3 台と Sentinel 3 台（それぞれ 1 つの AZ に固定。名前は Cloud Map の DNS）
  local kind port mount cpu mem script hc
  for i in 0 1 2; do
    for kind in valkey sentinel; do
      if [ $kind = valkey ]; then port=6379; mount=/data; cpu=512; mem=1024; script=$(valkey_script)
      else port=26379; mount=/conf; cpu=256; mem=512; script=$(sentinel_script); fi
      local K; K=$(printf '%s' "$kind" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')
cat <<EOF
  $K${i}Record:
    Type: AWS::ServiceDiscovery::Service
    Properties:
      Name: $kind-$i
      NamespaceId: !GetAtt Namespace.Id
      DnsConfig: {DnsRecords: [{Type: A, TTL: 10}], RoutingPolicy: MULTIVALUE}
      HealthCheckCustomConfig: {FailureThreshold: 1}
EOF
      task_head "$K${i}Task" "$kind-$i" $cpu $mem BasicRole
cat <<EOF
      Volumes:
        - Name: data
          EFSVolumeConfiguration:
            FilesystemId: !Ref Efs
            TransitEncryption: ENABLED
            AuthorizationConfig: {AccessPointId: !Ref Ap$kind$i, IAM: ENABLED}
      ContainerDefinitions:
        - Name: $kind
          Image: !Ref ImageValkey
          Essential: true
          EntryPoint: [sh, -c]
          Command:
            - |
$(printf '%s\n' "$script" | indent '              ')
          Environment:
            - {Name: ME, Value: !Sub "$kind-$i.\${Prefix}.internal"}
            - {Name: NS, Value: !Sub "\${Prefix}.internal"}
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvValkey}"}]
          PortMappings: [{ContainerPort: $port, Protocol: tcp}]
          MountPoints: [{SourceVolume: data, ContainerPath: $mount}]
          LinuxParameters: {InitProcessEnabled: true}
EOF
      if [ $kind = valkey ]; then
cat <<'EOF'
          HealthCheck:
            Command: [CMD-SHELL, "valkey-cli -a \"$REDIS_PASSWORD\" --no-auth-warning ping | grep -q PONG"]
            Interval: 10
            Timeout: 5
            Retries: 3
            StartPeriod: 30
EOF
      fi
      logcfg "$kind" "$kind-$i"
cat <<EOF
  $K${i}Service:
    Type: AWS::ECS::Service
    DependsOn: [EfsMount$i]
    Properties:
      ServiceName: $kind-$i
      Cluster: !Ref Cluster
      TaskDefinition: !Ref $K${i}Task
      LaunchType: FARGATE
      DesiredCount: 1
      EnableExecuteCommand: $(exec_flag)
      # 同じ EFS のディレクトリに 2 つのタスクが同時に書かないように、止めてから起こす
      DeploymentConfiguration: {MinimumHealthyPercent: 0, MaximumPercent: 100, DeploymentCircuitBreaker: {Enable: true, Rollback: true}}
$(service_net '      ' RedisSg "[$(subnet_n "$i")]")
      ServiceRegistries: [{RegistryArn: !GetAtt $K${i}Record.Arn}]
EOF
    done
  done
  # ---------------- 検索アプリと整形の定期処理
  local audit_env
  audit_env=$(cat <<EOF
            - {Name: AUDIT_S3_ENDPOINT, Value: ""}
            - {Name: AUDIT_S3_REGION, Value: !Ref AWS::Region}
            - {Name: AUDIT_S3_BUCKET_PREFIX, Value: !Sub "\${Prefix}-\${AWS::AccountId}-"}
            - {Name: AUDIT_S3_BUCKET, Value: ""}
            - {Name: AUDIT_ACCESS_RETENTION_DAYS, Value: ""}
            - {Name: AUDIT_RETENTION_MODE, Value: ""}
            - {Name: AUDIT_DUCKDB_MEMORY, Value: 512MB}
$(proxy_env '            ')
$(ca_env '            ' /etc/ssl/certs/ca-certificates.crt)
EOF
)
  task_head ViewerTask audit-viewer 512 1024 ViewerRole
cat <<EOF
      ContainerDefinitions:
        - Name: viewer
          Image: !Ref ImageAudit
          Essential: true
          Command: [python, -m, audit.viewer]
          PortMappings: [{ContainerPort: 8000, Protocol: tcp}]
          LinuxParameters: {InitProcessEnabled: true}
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvViewer}"}]
          Environment:
$audit_env
            - {Name: AUDIT_BASE_URL, Value: !Ref BaseUrl}
            - {Name: AUDIT_OIDC_ISSUER, Value: https://accounts.google.com}
            - {Name: AUDIT_ADMINS, Value: !Ref AuditAdmins}
            - {Name: AUDIT_AUDITORS, Value: !Ref AuditAuditors}
            - {Name: AUDIT_ROLE_CLAIM, Value: ""}
$(logcfg audit-viewer viewer)
  ViewerService:
    Type: AWS::ECS::Service
    DependsOn: [Https, RuleAuditAllow]
    Properties:
      ServiceName: audit-viewer
      Cluster: !Ref Cluster
      TaskDefinition: !Ref ViewerTask
      LaunchType: FARGATE
      DesiredCount: !If [GoogleOn, 2, 0]
      EnableExecuteCommand: $(exec_flag)
      HealthCheckGracePeriodSeconds: 60
      DeploymentConfiguration:
        MinimumHealthyPercent: 100
        MaximumPercent: 200
        DeploymentCircuitBreaker: {Enable: true, Rollback: true}
$(service_net '      ' ViewerSg)
      LoadBalancers: [{ContainerName: viewer, ContainerPort: 8000, TargetGroupArn: !Ref ViewerTg}]
EOF
  task_head CompactorTask audit-compactor 1024 3072 CompactorRole
cat <<EOF
      ContainerDefinitions:
        - Name: compactor
          Image: !Ref ImageAudit
          Essential: true
          Command: [python, -m, audit.compactor]
          Environment:
$audit_env
$(logcfg audit-compactor compactor)
  CompactorSchedule:
    Type: AWS::Scheduler::Schedule
    Properties:
      Name: !Sub "\${Prefix}-audit-compactor"
      ScheduleExpression: cron(10 * * * ? *)
      ScheduleExpressionTimezone: Asia/Tokyo
      FlexibleTimeWindow: {Mode: "OFF"}
      Target:
        Arn: !GetAtt Cluster.Arn
        RoleArn: !GetAtt SchedulerRole.Arn
        EcsParameters:
          TaskDefinitionArn: !Ref CompactorTask
          LaunchType: FARGATE
          NetworkConfiguration:
            AwsvpcConfiguration:
              Subnets: $(subnets_list)
              SecurityGroups: [!Ref InternalSg]
              AssignPublicIp: DISABLED
EOF
  # ---------------- google_workspace_mcp と偽 LLM
  task_head McpTask workspace-mcp 256 512 BasicRole
cat <<EOF
      ContainerDefinitions:
        - Name: workspace-mcp
          Image: !Ref ImageMcp
          Essential: true
          PortMappings: [{Name: http, ContainerPort: 8000, Protocol: tcp, AppProtocol: http}]
          EnvironmentFiles: [{Type: s3, Value: !Sub "arn:aws:s3:::\${ConfigBucket}/\${EnvMcp}"}]
          Environment:
            - {Name: MCP_ENABLE_OAUTH21, Value: "true"}
            - {Name: EXTERNAL_OAUTH21_PROVIDER, Value: "true"}
            - {Name: WORKSPACE_MCP_STATELESS_MODE, Value: "true"}
            - {Name: WORKSPACE_MCP_TOOLS, Value: gmail}
            - {Name: WORKSPACE_MCP_READ_ONLY, Value: "true"}
            - {Name: WORKSPACE_EXTERNAL_URL, Value: "http://workspace-mcp:8000"}
$(proxy_env '            ')
$(ca_env '            ' /etc/lab/ca-bundle.pem)
$(logcfg workspace-mcp mcp)
  McpService:
    Type: AWS::ECS::Service
    Properties:
      ServiceName: workspace-mcp
      Cluster: !Ref Cluster
      TaskDefinition: !Ref McpTask
      LaunchType: FARGATE
      DesiredCount: !If [GoogleOn, 1, 0]
      DeploymentConfiguration: {DeploymentCircuitBreaker: {Enable: true, Rollback: true}}
      EnableExecuteCommand: $(exec_flag)
$(service_net '      ' InternalSg)
      ServiceConnectConfiguration:
        Enabled: true
        Namespace: !GetAtt Namespace.Arn
        Services: [{PortName: http, DiscoveryName: workspace-mcp, ClientAliases: [{Port: 8000, DnsName: workspace-mcp}]}]
EOF
  task_head FakeTask fake-llm 256 512 BasicRole
cat <<EOF
      ContainerDefinitions:
        - Name: fake-llm
          Image: !Ref ImageFakeLlm
          Essential: true
          PortMappings: [{Name: http, ContainerPort: 8000, Protocol: tcp, AppProtocol: http}]
$(logcfg fake-llm fake)
  FakeService:
    Type: AWS::ECS::Service
    Properties:
      ServiceName: fake-llm
      Cluster: !Ref Cluster
      TaskDefinition: !Ref FakeTask
      LaunchType: FARGATE
      DesiredCount: !If [FakeOn, 2, 0]
      DeploymentConfiguration: {DeploymentCircuitBreaker: {Enable: true, Rollback: true}}
$(service_net '      ' InternalSg)
      ServiceConnectConfiguration:
        Enabled: true
        Namespace: !GetAtt Namespace.Arn
        Services: [{PortName: http, DiscoveryName: fake-llm, ClientAliases: [{Port: 8000, DnsName: fake-llm}]}]
EOF
  # ---------------- 監視の台（Zabbix proxy。検証では Zabbix 一式・中継・踏み台も）
  local relay=false sg_line=""
  [ "$INFRA" = managed ] && relay=true
  [ "$INFRA" = provided ] && sg_line="        SecurityGroupIds: [!Ref MonitorSg]"
cat <<EOF
  MonitorLaunch:
    Type: AWS::EC2::LaunchTemplate
    Properties:
      LaunchTemplateData:
        ImageId: !Ref MonitorAmi
        InstanceType: !Ref MonitorType
        IamInstanceProfile: {Arn: !GetAtt MonitorProfile.Arn}
$sg_line
        # Zabbix の AWS 用のテンプレートの role_base は IMDSv1 の形で資格情報を読む（ZBX-27315）
        MetadataOptions: {HttpTokens: optional, HttpPutResponseHopLimit: 2, HttpEndpoint: enabled}
        BlockDeviceMappings: [{DeviceName: /dev/sda1, Ebs: {VolumeSize: 30, VolumeType: gp3, Encrypted: true}}]
        TagSpecifications:
          - ResourceType: instance
            Tags: [{Key: Name, Value: !Sub "\${Prefix}-monitor"}, {Key: "lab:role", Value: !Sub "\${Prefix}-monitor"}]
        UserData:
          Fn::Base64: !Sub
            - |
$(monitor_init | indent '              ')
            - {ProxyPort: "3128", Relay: "$relay", ZbxServer: !If [LocalZabbix, "127.0.0.1", !Ref ZabbixServer], LocalZbx: !If [LocalZabbix, "true", "false"]}
EOF
  if [ "$INFRA" = managed ]; then
cat <<'EOF'
  # 台は作り直せるように自分の ENI を持つ（別の ENI に付けると、作り直しで新旧の台が取り合う）。
  # 起動の直後から外に出られるよう自動の公開 IP を付け、固定の Elastic IP をすぐ結び付ける（初期化は取得を繰り返して待つ）
  Monitor:
    Type: AWS::EC2::Instance
    DependsOn: [RelayDefault, RelaySubnetRoutes]
    Properties:
      LaunchTemplate: {LaunchTemplateId: !Ref MonitorLaunch, Version: !GetAtt MonitorLaunch.LatestVersionNumber}
      NetworkInterfaces: [{DeviceIndex: "0", SubnetId: !Ref RelaySubnet, GroupSet: [!Ref MonitorSg], AssociatePublicIpAddress: true}]
  MonitorEip:
    Type: AWS::EC2::EIP
    DependsOn: IgwAttach
    Properties: {Domain: vpc}
  MonitorEipAssoc:
    Type: AWS::EC2::EIPAssociation
    Properties: {AllocationId: !GetAtt MonitorEip.AllocationId, InstanceId: !Ref Monitor}
EOF
  else
cat <<'EOF'
  MonitorGroup:
    Type: AWS::AutoScaling::AutoScalingGroup
    Properties:
      MinSize: "1"
      MaxSize: "1"
      LaunchTemplate: {LaunchTemplateId: !Ref MonitorLaunch, Version: !GetAtt MonitorLaunch.LatestVersionNumber}
      VPCZoneIdentifier: !Ref SubnetIds
      HealthCheckType: EC2
    UpdatePolicy:
      AutoScalingRollingUpdate: {MinInstancesInService: 0, MaxBatchSize: 1}
EOF
  fi
cat <<EOF

Outputs:
  ClusterName: {Value: !Ref Cluster}
  AlbDns: {Value: !GetAtt Alb.DNSName}
  LitellmTargetGroup: {Value: !Ref LitellmTg}
  RdsId: {Value: !Ref Db}
  RdsEndpoint: {Value: !GetAtt Db.Endpoint.Address}
  RdsResourceId: {Value: !GetAtt Db.DbiResourceId}
  MigrateTaskDefinition: {Value: !Ref MigrateTask}
  MigrateSecurityGroup: {Value: !Ref MigrateSg}
  PrivateSubnets: {Value: !Join [",", $(subnets_list)]}
  LitellmSecurityGroup: {Value: !Ref LitellmSg}
  InternalSecurityGroup: {Value: !Ref InternalSg}
  RedisSecurityGroup: {Value: !Ref RedisSg}
  Namespace: {Value: !Sub "\${Prefix}.internal"}
  RawBucket: {Value: !Ref RawBucket}
  AccessBucket: {Value: !Ref AccessBucket}
  DerivedBucket: {Value: !Ref DerivedBucket}
EOF
  if [ "$INFRA" = managed ]; then
cat <<'EOF'
  MonitorInstanceId: {Value: !Ref Monitor}
  MonitorPublicIp: {Value: !Ref MonitorEip}
  MonitorPrivateIp: {Value: !GetAtt Monitor.PrivateIp}
EOF
  fi
}

# ---------------------------------------------------------------- 入口
# 関数にまとめて最後に exit する（流している間にこのファイルを書き換えても、続きを読み違えないように）
main() {
  case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    smoke) preflight; smoke ;;
    forward) forward ;;
    zabbix) preflight; zabbix_register ;;
    render-config) render_config ;;
    render-template) render_template ;;
    render-zabbix) zbx_import_yaml ;;
    render-billing) billing_json contracts; echo; billing_json fx_rates; echo ;;
    *) echo "使い方: $0 <up|down|status|smoke|forward|zabbix>" >&2; exit 2 ;;
  esac
}
main "$@"; exit $?
