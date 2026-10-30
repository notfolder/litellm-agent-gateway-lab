# GCP 固有の手順（vm/node-init.sh の HOOKS の印に差し込む。labtool/clouds/gcp.py が @@名前@@ を埋める）。
# gcloud は使わず、メタデータサーバーのトークンで Compute Engine の REST API を呼ぶ（中継があれば curl が環境変数で通る。
# メタデータサーバーだけは中継を通さない）。
LAB_PROJECT="@@PROJECT@@"
LAB_REGION="@@REGION@@"
LAB_ADDRESS_NAME="@@ADDRESS_NAME@@"          # 静的な外部 IP の名前
LAB_META="http://metadata.google.internal/computeMetadata/v1"
LAB_COMPUTE="https://compute.googleapis.com/compute/v1"

lab_hook_values() {
  echo "LAB_PROJECT='${LAB_PROJECT}'"
  echo "LAB_REGION='${LAB_REGION}'"
  echo "LAB_ADDRESS_NAME='${LAB_ADDRESS_NAME}'"
  echo "LAB_META='${LAB_META}'"
  echo "LAB_COMPUTE='${LAB_COMPUTE}'"
}

lab_install_tools() {  # curl と python3（JSON の読み取り）は Ubuntu の公式イメージに入っている
  command -v python3 >/dev/null 2>&1 || apt-get install -y -q python3
}

lab_token() {  # VM のサービスアカウントのアクセストークン
  curl -s --noproxy '*' -H 'Metadata-Flavor: Google' "${LAB_META}/instance/service-accounts/default/token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

lab_meta() {  # 引数: instance/ 以下のパス
  curl -s --noproxy '*' -H 'Metadata-Flavor: Google' "${LAB_META}/instance/$1"
}

lab_self_id() {  # <ゾーン>/<名前>
  local zone
  zone="$(lab_meta zone)"       # projects/<番号>/zones/<ゾーン>
  echo "${zone##*/}/$(lab_meta name)"
}

lab_api() {  # 引数: メソッド、URL、（JSON の本文）
  local method="$1" url="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sf -X "$method" -H "Authorization: Bearer $(lab_token)" -H 'Content-Type: application/json' "$url" -d "$body"
  else
    curl -sf -X "$method" -H "Authorization: Bearer $(lab_token)" "$url"
  fi
}

lab_wait_op() {  # 引数: 操作の selfLink。終わるまで待つ
  local link="$1" status
  for _ in $(seq 1 60); do
    status="$(lab_api GET "$link" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
    [ "$status" = "DONE" ] && return 0
    sleep 2
  done
  return 1
}

lab_access_config_ip() {  # 引数: ゾーン、インスタンス名。今の外部 IP（無ければ空）
  lab_api GET "${LAB_COMPUTE}/projects/${LAB_PROJECT}/zones/$1/instances/$2" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin); ac=(d.get("networkInterfaces") or [{}])[0].get("accessConfigs") or []
print(ac[0].get("natIP","") if ac else "")'
}

lab_delete_access_config() {  # 引数: ゾーン、インスタンス名
  local op
  op="$(lab_api POST "${LAB_COMPUTE}/projects/${LAB_PROJECT}/zones/$1/instances/$2/deleteAccessConfig?accessConfig=External%20NAT&networkInterface=nic0" "" || true)"
  [ -n "$op" ] && lab_wait_op "$(echo "$op" | python3 -c 'import json,sys; print(json.load(sys.stdin)["selfLink"])')"
}

lab_add_access_config() {  # 引数: ゾーン、インスタンス名、（固定の IP。空なら一時的な IP）
  local body op
  if [ -n "${3:-}" ]; then
    body="{\"name\":\"External NAT\",\"type\":\"ONE_TO_ONE_NAT\",\"natIP\":\"$3\"}"
  else
    body='{"name":"External NAT","type":"ONE_TO_ONE_NAT"}'
  fi
  op="$(lab_api POST "${LAB_COMPUTE}/projects/${LAB_PROJECT}/zones/$1/instances/$2/addAccessConfig?networkInterface=nic0" "$body")"
  lab_wait_op "$(echo "$op" | python3 -c 'import json,sys; print(json.load(sys.stdin)["selfLink"])')"
}

lab_take_entry_ip() {  # 主系になったとき: 静的 IP を今の持ち主から外し（その台には一時的な IP を付け直し）、自分に付ける
  local self zone name addr ip users holder hz hn i
  self="$(lab_self_id)"; zone="${self%%/*}"; name="${self##*/}"
  addr="$(lab_api GET "${LAB_COMPUTE}/projects/${LAB_PROJECT}/regions/${LAB_REGION}/addresses/${LAB_ADDRESS_NAME}")"
  ip="$(echo "$addr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["address"])')"
  users="$(echo "$addr" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin).get("users",[])))')"
  for i in 1 2 3 4 5 6; do
    for holder in $users; do   # .../zones/<ゾーン>/instances/<名前>
      hz="$(echo "$holder" | awk -F/ '{print $(NF-2)}')"; hn="${holder##*/}"
      [ "$hz/$hn" = "$self" ] && continue
      lab_delete_access_config "$hz" "$hn" || true
      lab_add_access_config "$hz" "$hn" "" || true
    done
    if [ "$(lab_access_config_ip "$zone" "$name")" != "$ip" ]; then
      lab_delete_access_config "$zone" "$name" || true
      if lab_add_access_config "$zone" "$name" "$ip"; then
        logger -t lab-entry-ip "took ${ip} on ${self}"
        return 0
      fi
    else
      return 0
    fi
    sleep 5
  done
  logger -t lab-entry-ip "failed to take ${ip}"
  lab_ensure_egress || true
  return 1
}

lab_ensure_egress() {  # 自分に外部 IP が無ければ一時的なものを付ける（主系を降りた台、起動直後の台の外向きの通信を保つ）
  local self zone name
  self="$(lab_self_id)"; zone="${self%%/*}"; name="${self##*/}"
  if [ -z "$(lab_access_config_ip "$zone" "$name")" ]; then
    lab_add_access_config "$zone" "$name" "" && logger -t lab-entry-ip "restored an ephemeral external IP on ${self}"
  fi
  return 0
}
