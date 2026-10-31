#!/bin/bash
# VM ノードの初期化（LAB_TARGET=aws-ec2 / gcp-vm。labtool/vm.py が SSH で `sudo bash -s` に流す。
# 変更設計書 .history/2026-10-31-内部LBとVM側の防御とZabbix/ 4 章）。Ubuntu 24.04 LTS（arm64 / amd64）。
# labtool/vm.py が @@名前@@ を置き換える。クラウド固有の手順は無い（VM はクラウドの API を呼ばない。入口は内部ロードバランサ）。
# 3 台で同じ内容で、台ごとの値だけが違う。k3s の参加トークンと版は SSH で渡す（VM にバケットの権限は要らない）。
# 2 回目以降（/var/lib/lab/ready がある）は、CA・中継・nftables・Zabbix agent の設定だけ直して抜ける。
set -euo pipefail
exec > >(tee -a /var/log/lab-node-init.log) 2>&1

NODE_INDEX="@@NODE_INDEX@@"            # 1〜3
SELF_IP="@@SELF_IP@@"                  # 自分のプライベート IP（固定）
FIRST_IP="@@FIRST_IP@@"                # 1 番の台のプライベート IP
PEER_IPS="@@PEER_IPS@@"                # 他の 2 台のプライベート IP（空白区切り）
NODE_IPS="@@NODE_IPS@@"                # 3 台のプライベート IP（カンマ区切り。nftables の集合）
REGISTRY_HOST="@@REGISTRY_HOST@@"      # クラスタの中のイメージ置き場の名前（例 lab-registry:5000）
REGISTRY_NODEPORT="@@REGISTRY_NODEPORT@@"
SSH_PUBKEY="@@SSH_PUBKEY@@"            # 構築ツールの SSH の公開鍵（空なら触らない）
SSH_USER="@@SSH_USER@@"
K3S_VERSION="@@K3S_VERSION@@"          # 例 v1.35.5+k3s1
K3S_TOKEN="@@K3S_TOKEN@@"              # 参加トークン（初回だけ使う。ディスクには k3s 自身の保存先だけ）
EGRESS_PROXY="@@EGRESS_PROXY@@"        # 出口プロキシの URL（空なら中継なし。外向きの遮断もしない）
EGRESS_NO_PROXY="@@EGRESS_NO_PROXY@@"  # 中継を通さない宛先
EGRESS_CA_B64="@@EGRESS_CA_B64@@"      # 中継の CA（PEM の base64。空なら無し）
# ---- nftables（変更設計書 4.1 節）。CIDR はカンマ区切り。空なら「無し」 ----
VPC_CIDR="@@VPC_CIDR@@"                # 利用者と内部ロードバランサがいる範囲（80/443 を通す）
LB_IP="@@LB_IP@@"                      # 内部ロードバランサの IP（外向きの許可。Pod が自分の入口を呼ぶ）
HC_RANGES="@@HC_RANGES@@"              # ロードバランサのヘルスチェックの送信元
ADMIN_CIDRS="@@ADMIN_CIDRS@@"          # 22 を通す送信元（踏み台。managed では操作者の IP も）
EGRESS_CIDRS="@@EGRESS_CIDRS@@"        # 中継を通さずに直接届いてよい宛先（中継、踏み台、メタデータ・DNS・NTP）
POD_CIDR="@@POD_CIDR@@"                # k3s の Pod 網（10.42.0.0/16）
SVC_CIDR="@@SVC_CIDR@@"                # k3s の Service 網（10.43.0.0/16）
# ---- Zabbix agent 2（変更設計書 4.2 節） ----
ZABBIX_SERVER="@@ZABBIX_SERVER@@"      # サーバーの宛先 host:port（空なら agent を入れない）
ZABBIX_HOSTNAME="@@ZABBIX_HOSTNAME@@"  # このホストの名前（VM の名前）
ZABBIX_METADATA="@@ZABBIX_METADATA@@"  # 自動登録の目印（litellm-lab-node）
ZABBIX_RELEASE_URL="@@ZABBIX_RELEASE_URL@@"

CA_PATH=/usr/local/share/ca-certificates/lab-egress.crt
mkdir -p /var/lib/lab /etc/rancher/k3s /usr/local/bin
export DEBIAN_FRONTEND=noninteractive

# 構築ツールの SSH の鍵（provision が置いた鍵と同じ行があっても害は無い）
if [ -n "$SSH_PUBKEY" ]; then
  home="$(getent passwd "$SSH_USER" | cut -d: -f6)"
  install -d -m 700 -o "$SSH_USER" -g "$SSH_USER" "$home/.ssh"
  grep -qF "$SSH_PUBKEY" "$home/.ssh/authorized_keys" 2>/dev/null || echo "$SSH_PUBKEY" >> "$home/.ssh/authorized_keys"
  chmod 600 "$home/.ssh/authorized_keys" && chown "$SSH_USER:$SSH_USER" "$home/.ssh/authorized_keys"
fi

# ---- 出口プロキシ（Zscaler 相当）: CA を OS に信頼させ、中継を apt / Docker / シェル / k3s に伝える ----
if [ -n "$EGRESS_CA_B64" ]; then
  echo "$EGRESS_CA_B64" | base64 -d > "$CA_PATH"
  update-ca-certificates >/dev/null
fi
if [ -n "$EGRESS_PROXY" ]; then
  export HTTP_PROXY="$EGRESS_PROXY" HTTPS_PROXY="$EGRESS_PROXY" NO_PROXY="$EGRESS_NO_PROXY"
  export http_proxy="$EGRESS_PROXY" https_proxy="$EGRESS_PROXY" no_proxy="$EGRESS_NO_PROXY"
  cat > /etc/apt/apt.conf.d/90lab-proxy <<EOT
Acquire::http::Proxy "${EGRESS_PROXY}";
Acquire::https::Proxy "${EGRESS_PROXY}";
EOT
  # 操作者がログインしたシェルと systemd のサービス（docker、k3s）向け
  sed -i '/^\(HTTP_PROXY\|HTTPS_PROXY\|NO_PROXY\|http_proxy\|https_proxy\|no_proxy\)=/d' /etc/environment
  cat >> /etc/environment <<EOT
HTTP_PROXY=${EGRESS_PROXY}
HTTPS_PROXY=${EGRESS_PROXY}
NO_PROXY=${EGRESS_NO_PROXY}
http_proxy=${EGRESS_PROXY}
https_proxy=${EGRESS_PROXY}
no_proxy=${EGRESS_NO_PROXY}
EOT
  mkdir -p /etc/systemd/system/docker.service.d
  cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<EOT
[Service]
Environment="HTTP_PROXY=${EGRESS_PROXY}" "HTTPS_PROXY=${EGRESS_PROXY}" "NO_PROXY=${EGRESS_NO_PROXY}"
EOT
  # k3s（containerd）。インストールスクリプトも同じ値を書くが、2 回目以降の変更にも効くように毎回書く
  cat > /etc/systemd/system/k3s.service.env <<EOT
HTTP_PROXY=${EGRESS_PROXY}
HTTPS_PROXY=${EGRESS_PROXY}
NO_PROXY=${EGRESS_NO_PROXY}
EOT
  chmod 600 /etc/systemd/system/k3s.service.env
else
  rm -f /etc/apt/apt.conf.d/90lab-proxy /etc/systemd/system/docker.service.d/http-proxy.conf
fi

# ---- nftables: クラウドのファイアウォールに頼らず、この台で守る（変更設計書 4.1 節） ----
# kube-proxy と flannel は iptables（nf_tables 後端）に自分の規則を書く。この表は別なので共存する。
# こちらの drop は最終なので、Kubernetes が通したいもの（Pod 網、ServiceLB の DNAT、ClusterIP）は上の accept で拾う。
# 入りの絞り込みは prerouting（DNAT の前）で行う。ServiceLB（80/443）と NodePort は DNAT されて forward を通るため。
set_elems() {  # カンマ区切り → nftables の集合の要素（空なら誰にも当たらないダミー）
  local v="${1//,/, }"
  [ -n "$v" ] && echo "$v" || echo "127.0.0.255/32"
}
IFACE="$(ip -o -4 route show to default | awk '{print $5}' | head -1)"
if [ -n "$EGRESS_PROXY" ]; then
  EGRESS_ELEMS="$(set_elems "${EGRESS_CIDRS}${LB_IP:+,$LB_IP}")"
else
  EGRESS_ELEMS="0.0.0.0/0"    # 中継が無い環境（透過の Zscaler など）は外向きを遮断しない
fi
HC_ELEMS="$(set_elems "$HC_RANGES")"
ADMIN_ELEMS="$(set_elems "$ADMIN_CIDRS")"
NODE_ELEMS="$(set_elems "$NODE_IPS")"
INTERNAL_ELEMS="$(set_elems "${VPC_CIDR},${POD_CIDR},${SVC_CIDR}")"
ZABBIX_SERVER_IP="${ZABBIX_SERVER%%:*}"
ZABBIX_ELEMS="$(set_elems "${ZABBIX_SERVER_IP:+$ZABBIX_SERVER_IP/32,}${POD_CIDR}")"
apt-get install -y -q nftables >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q nftables; }
cat > /etc/nftables.conf <<EOT
#!/usr/sbin/nft -f
# litellm-lab（vm/node-init.sh が書く。手で編集しない）。この表だけを作り直す（flush ruleset は kube-proxy と flannel の
# iptables の規則まで消してしまう）
add table inet lab
delete table inet lab
table inet lab {
  set nodes     { type ipv4_addr; flags interval; elements = { ${NODE_ELEMS} } }
  set hc        { type ipv4_addr; flags interval; elements = { ${HC_ELEMS} } }
  set admins    { type ipv4_addr; flags interval; elements = { ${ADMIN_ELEMS} } }
  set internal  { type ipv4_addr; flags interval; elements = { ${INTERNAL_ELEMS} } }
  set egress_ok { type ipv4_addr; flags interval; elements = { ${EGRESS_ELEMS} } }
  set zabbix    { type ipv4_addr; flags interval; elements = { ${ZABBIX_ELEMS} } }

  chain ingress_filter {
    # DNAT の前（priority -150）。入りの許可はここだけで決める
    type filter hook prerouting priority -150; policy accept;
    iif lo accept
    iifname { "cni0", "flannel.1", "docker0" } accept
    ct state established,related accept
    ct state invalid drop
    ip protocol icmp accept
    udp sport 67 udp dport 68 accept
    tcp dport 22 ip saddr @admins accept
    tcp dport { 80, 443 } ip saddr @internal accept
    tcp dport { 80, 443 } ip saddr @hc accept
    tcp dport { 6443, 2379, 2380, 10250, 10257, 10259, ${REGISTRY_NODEPORT} } ip saddr @nodes accept
    udp dport 8472 ip saddr @nodes accept
    tcp dport 10250 ip saddr ${POD_CIDR} accept
    tcp dport 10050 ip saddr @zabbix accept
    counter drop
  }
  chain forward {
    # Pod と Docker のビルド（docker0。イメージのビルドの pip など）の外向き（中継と内側だけ）と、DNAT された入り
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
    oifname { "cni0", "flannel.1", "docker0" } accept
    iifname { "cni0", "flannel.1", "docker0" } ip daddr @internal accept
    iifname { "cni0", "flannel.1", "docker0" } ip daddr @egress_ok accept
    iifname { "cni0", "flannel.1", "docker0" } ip daddr @nodes accept
    counter drop
  }
  chain output {
    # この台の外向き（中継と内側だけ）
    type filter hook output priority 0; policy drop;
    oif lo accept
    oifname { "cni0", "flannel.1", "docker0" } accept
    ct state established,related accept
    ip protocol icmp accept
    udp dport 67 accept
    ip daddr @internal accept
    ip daddr @nodes accept
    ip daddr @egress_ok accept
    counter drop
  }
}
EOT
nft -c -f /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1 || true
nft -f /etc/nftables.conf
echo "nftables applied (egress: ${EGRESS_ELEMS})"

# ---- Zabbix agent 2（能動で送る。サーバーが無ければ入れない） ----
if [ -n "$ZABBIX_SERVER" ]; then
  if ! command -v zabbix_agent2 >/dev/null 2>&1; then
    # Zabbix の公式リポジトリは arm64 が別（ubuntu-arm64/。配備で判明 2026-10-31）。違う方が入っていれば入れ替える
    if [ "$(dpkg --print-architecture)" = "arm64" ]; then
      ZABBIX_RELEASE_URL="$(echo "$ZABBIX_RELEASE_URL" | sed 's#/ubuntu/#/ubuntu-arm64/#')"
      if dpkg -s zabbix-release >/dev/null 2>&1 && ! grep -rqs "ubuntu-arm64" /etc/apt/sources.list.d/; then
        apt-get purge -y -q zabbix-release >/dev/null; rm -f /etc/apt/sources.list.d/zabbix*
      fi
    fi
    if ! dpkg -s zabbix-release >/dev/null 2>&1; then
      curl -sfL -o /tmp/zabbix-release.deb "$ZABBIX_RELEASE_URL"
      dpkg -i /tmp/zabbix-release.deb >/dev/null
      rm -f /tmp/zabbix-release.deb
    fi
    apt-get update -q
    apt-get install -y -q zabbix-agent2
  fi
  # 値に / が入る（Pod 網の CIDR）ので区切りは |
  sed -i -e "s|^Server=.*|Server=${ZABBIX_SERVER_IP}${POD_CIDR:+,$POD_CIDR}|" \
         -e "s|^ServerActive=.*|ServerActive=${ZABBIX_SERVER}|" \
         -e "s|^Hostname=.*|Hostname=${ZABBIX_HOSTNAME}|" \
         -e "s|^# HostMetadata=.*|HostMetadata=${ZABBIX_METADATA}|" \
         -e "s|^HostMetadata=.*|HostMetadata=${ZABBIX_METADATA}|" /etc/zabbix/zabbix_agent2.conf
  grep -q "^HostMetadata=" /etc/zabbix/zabbix_agent2.conf || echo "HostMetadata=${ZABBIX_METADATA}" >> /etc/zabbix/zabbix_agent2.conf
  systemctl enable zabbix-agent2 >/dev/null 2>&1 || true
  systemctl restart zabbix-agent2
  echo "zabbix agent 2 -> ${ZABBIX_SERVER} as ${ZABBIX_HOSTNAME}"
else
  systemctl disable --now zabbix-agent2 >/dev/null 2>&1 || true
fi

# ---- k3s の追加の設定: コントローラーとスケジューラーの指標を、別の台の zabbix-proxy から読めるように全体で待ち受ける ----
# （既定は 127.0.0.1。認証と認可は付いたまま。nftables は 3 台と Pod 網からだけ 10257 / 10259 を通す。変更設計書 2026-10-31-Zabbixの監視の一揃い）
# `+` は config.yaml の同じ引数に足す（置き換えない）
mkdir -p /etc/rancher/k3s/config.yaml.d
METRICS_CONF=/etc/rancher/k3s/config.yaml.d/50-lab-metrics.yaml
METRICS_WANT='kube-controller-manager-arg+:
  - bind-address=0.0.0.0
kube-scheduler-arg+:
  - bind-address=0.0.0.0'
METRICS_CHANGED=""
if [ "$(cat "$METRICS_CONF" 2>/dev/null)" != "$METRICS_WANT" ]; then
  printf '%s\n' "$METRICS_WANT" > "$METRICS_CONF"
  chmod 600 "$METRICS_CONF"
  METRICS_CHANGED=1
fi

if [ -f /var/lib/lab/ready ]; then
  systemctl daemon-reload || true
  if systemctl is-active --quiet docker; then systemctl restart docker || true; fi
  if [ -n "$METRICS_CHANGED" ] && systemctl is-active --quiet k3s; then
    # 設定が変わったら k3s を再起動する（コンテナは止まらない）。API の準備を待ってから抜ける（./lab は台を 1 台ずつ流す）
    systemctl restart k3s
    for _ in $(seq 1 60); do
      if /usr/local/bin/k3s kubectl get --raw /readyz >/dev/null 2>&1; then break; fi
      sleep 5
    done
    echo "k3s restarted for the metrics settings"
  fi
  echo "already initialized (proxy, ca, nftables, zabbix and k3s settings refreshed)"; exit 0
fi

# ---- 道具 ----
apt-get update -q
apt-get install -y -q curl ca-certificates docker.io
systemctl daemon-reload
systemctl enable --now docker
usermod -aG docker "$SSH_USER" || true

# ---- イメージ置き場（クラスタの中の registry）を、各ノードが自分の NodePort 経由で引く ----
cat > /etc/rancher/k3s/registries.yaml <<EOT
mirrors:
  "${REGISTRY_HOST}":
    endpoint:
      - "http://${SELF_IP}:${REGISTRY_NODEPORT}"
EOT
REGISTRY_NAME="${REGISTRY_HOST%%:*}"
grep -q " ${REGISTRY_NAME}\$" /etc/hosts || echo "127.0.0.1 ${REGISTRY_NAME}" >> /etc/hosts

# ---- k3s ----
[ -n "$K3S_VERSION" ] && [ -n "$K3S_TOKEN" ] || { echo "k3s version or token is missing"; exit 1; }

alive() {  # 6443 が何か答えるか（k3s の server が起きているか）
  local code
  code="$(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "https://$1:6443/ping" || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

# 参加先を探す。誰か答えたらそこへ参加（1 台を作り直したときに、別のクラスタを作ってしまわない）。
# 誰も答えないとき、1 番なら新しいクラスタを起こし、2 番と 3 番は 1 番が起きるまで待つ
JOIN=""
for ip in $PEER_IPS; do
  if alive "$ip"; then JOIN="$ip"; break; fi
done
if [ -z "$JOIN" ] && [ "$NODE_INDEX" != "1" ]; then
  echo "waiting for the first node ($FIRST_IP)"
  for _ in $(seq 1 180); do
    if alive "$FIRST_IP"; then JOIN="$FIRST_IP"; break; fi
    sleep 5
  done
  [ -n "$JOIN" ] || { echo "the first node did not come up"; exit 1; }
fi

# 引数は設定ファイルに書く（コマンドラインに書くと、版上げでインストールスクリプトを再実行したときに消える）。
# 内容は Mac の k3d と同じ（ノードが止まったら 30 秒で Pod を移す）。Traefik と ServiceLB は同梱のものを使う。
# 参加先（server）と cluster-init は初回だけ意味を持ち、etcd のデータがある台の再起動では無視される
{
  echo "node-ip: ${SELF_IP}"
  echo "kube-apiserver-arg:"
  echo "  - default-not-ready-toleration-seconds=30"
  echo "  - default-unreachable-toleration-seconds=30"
  echo "kube-controller-manager-arg:"
  echo "  - node-monitor-grace-period=20s"
  if [ -n "$JOIN" ]; then
    echo "server: https://${JOIN}:6443"
  else
    echo "cluster-init: true"
  fi
} > /etc/rancher/k3s/config.yaml
chmod 600 /etc/rancher/k3s/config.yaml
if [ -n "$JOIN" ]; then echo "joining the cluster via $JOIN"; else echo "initializing a new cluster"; fi
# トークンは参加のときだけ環境変数で渡す（server/token に保存され、以後の再起動と版上げでは要らない）。
# 中継の環境変数（HTTP_PROXY など）は、インストールスクリプトが k3s.service.env に写す
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" K3S_TOKEN="$K3S_TOKEN" sh -s - server
unset K3S_TOKEN

for _ in $(seq 1 120); do
  if /usr/local/bin/k3s kubectl get --raw /readyz >/dev/null 2>&1; then break; fi
  sleep 5
done

touch /var/lib/lab/ready
echo "node $NODE_INDEX ready"
