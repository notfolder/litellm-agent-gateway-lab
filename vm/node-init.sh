#!/bin/bash
# VM ノードの初期化（LAB_TARGET=aws-ec2 / gcp-vm。labtool/vm.py が SSH で `sudo bash -s` に流す。
# 変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 5 章）。Ubuntu 24.04 LTS（arm64 / amd64）。
# labtool/vm.py が @@名前@@ を置き換え、クラウド固有の手順（vm/hooks-aws.sh / hooks-gcp.sh）を HOOKS の印に差し込む。
# 3 台で同じ内容で、台ごとの値だけが違う。k3s の参加トークンと版は SSH で渡す（VM にバケットの権限は要らない）。
# 2 回目以降（/var/lib/lab/ready がある）は、CA と中継の設定だけ直して抜ける。
set -euo pipefail
exec > >(tee -a /var/log/lab-node-init.log) 2>&1

NODE_INDEX="@@NODE_INDEX@@"            # 1〜3
SELF_IP="@@SELF_IP@@"                  # 自分のプライベート IP（固定）
FIRST_IP="@@FIRST_IP@@"                # 1 番の台のプライベート IP
PEER_IPS="@@PEER_IPS@@"                # 他の 2 台のプライベート IP（空白区切り）
PRIORITY="@@PRIORITY@@"                # keepalived の優先度（1 番が高い）
REGISTRY_HOST="@@REGISTRY_HOST@@"      # クラスタの中のイメージ置き場の名前（例 lab-registry:5000）
REGISTRY_NODEPORT="@@REGISTRY_NODEPORT@@"
SSH_PUBKEY="@@SSH_PUBKEY@@"            # 構築ツールの SSH の公開鍵（空なら触らない）
K3S_VERSION="@@K3S_VERSION@@"          # 例 v1.35.5+k3s1
K3S_TOKEN="@@K3S_TOKEN@@"              # 参加トークン（初回だけ使う。ディスクには k3s 自身の保存先だけ）
EGRESS_PROXY="@@EGRESS_PROXY@@"        # 出口プロキシの URL（空なら中継なし）
EGRESS_NO_PROXY="@@EGRESS_NO_PROXY@@"  # 中継を通さない宛先
EGRESS_CA_B64="@@EGRESS_CA_B64@@"      # 中継の CA（PEM の base64。空なら無し）
SSH_USER="@@SSH_USER@@"

# ---- クラウド固有の手順（関数: lab_install_tools、lab_self_id、lab_take_entry_ip、lab_ensure_egress、lab_hook_values） ----
@@HOOKS@@
# ---- ここからクラウドを知らない ----

CA_PATH=/usr/local/share/ca-certificates/lab-egress.crt
mkdir -p /var/lib/lab /etc/rancher/k3s /etc/keepalived /usr/local/bin

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

if [ -f /var/lib/lab/ready ]; then
  systemctl daemon-reload || true
  if systemctl is-active --quiet docker; then systemctl restart docker || true; fi
  echo "already initialized (proxy and ca settings refreshed)"; exit 0
fi

# ---- 道具 ----
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q keepalived curl ca-certificates docker.io
systemctl daemon-reload
systemctl enable --now docker
usermod -aG docker "$SSH_USER" || true
lab_install_tools   # hooks: クラウドの API を呼ぶ道具（AWS は aws-cli の snap。GCP は curl で足りる）

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

# ---- keepalived: 浮動 IP の持ち主を 3 台の多数決で決める（ユニキャスト VRRP） ----
IFACE="$(ip -o -4 route show to default | awk '{print $5}' | head -1)"

cat > /usr/local/bin/lab-health.sh <<'EOT'
#!/bin/bash
# この台が入口として健全か: k3s が動き、Traefik（80）が応答する
systemctl is-active --quiet k3s || exit 1
curl -s --noproxy '*' -o /dev/null --max-time 2 http://127.0.0.1:80/ || exit 1
exit 0
EOT

# 主系になったとき（notify_master）と、主系を降りたとき（notify_backup）に呼ぶ。中身はクラウド固有の関数
cat > /usr/local/bin/lab-take-entry-ip.sh <<EOT
#!/bin/bash
source /var/lib/lab/hooks.sh
lab_take_entry_ip
EOT
cat > /usr/local/bin/lab-release-entry-ip.sh <<EOT
#!/bin/bash
source /var/lib/lab/hooks.sh
lab_ensure_egress
EOT
# hooks の関数（補助の関数も含めて全部）と値を、keepalived の通知スクリプトと起動時のサービスが読む形で残す。
# 中継の環境変数と CA も書く（通知スクリプトは環境変数を継がない）
{
  declare -f
  lab_hook_values
  if [ -n "$EGRESS_PROXY" ]; then
    echo "export HTTP_PROXY='${EGRESS_PROXY}' HTTPS_PROXY='${EGRESS_PROXY}' NO_PROXY='${EGRESS_NO_PROXY}'"
    echo "export http_proxy='${EGRESS_PROXY}' https_proxy='${EGRESS_PROXY}' no_proxy='${EGRESS_NO_PROXY}'"
    echo "export AWS_CA_BUNDLE='/etc/ssl/certs/ca-certificates.crt' CURL_CA_BUNDLE='/etc/ssl/certs/ca-certificates.crt'"
  fi
} > /var/lib/lab/hooks.sh
chmod 755 /usr/local/bin/lab-health.sh /usr/local/bin/lab-take-entry-ip.sh /usr/local/bin/lab-release-entry-ip.sh
chmod 600 /var/lib/lab/hooks.sh

PEER_BLOCK=""
for ip in $PEER_IPS; do PEER_BLOCK="${PEER_BLOCK}    ${ip}
"; done
cat > /etc/keepalived/keepalived.conf <<EOT
global_defs {
  enable_script_security
  script_user root
}
vrrp_script chk_lab {
  script "/usr/local/bin/lab-health.sh"
  interval 2
  fall 3
  rise 2
  weight -100
}
vrrp_instance LAB {
  # 3 台とも BACKUP で始め、nopreempt で「戻った台が奪い返さない」。優先度は最初の持ち主を決めるためだけ
  state BACKUP
  nopreempt
  interface ${IFACE}
  virtual_router_id 60
  priority ${PRIORITY}
  advert_int 1
  unicast_src_ip ${SELF_IP}
  unicast_peer {
${PEER_BLOCK}  }
  # 仮想 IP は使わないリンクローカルのダミー。実際の経路は浮動 IP の付け替えで切り替わる
  virtual_ipaddress {
    169.254.60.1/32 dev ${IFACE}
  }
  track_script {
    chk_lab
  }
  notify_master "/usr/local/bin/lab-take-entry-ip.sh"
  notify_backup "/usr/local/bin/lab-release-entry-ip.sh"
}
EOT
systemctl enable --now keepalived

# ---- 起動時に外向きの IP を戻す（GCP: 主系を降りた台や止めて起こした台。AWS は何もしない）。startup-script の代わり ----
cat > /etc/systemd/system/lab-egress.service <<EOT
[Unit]
Description=litellm-lab: restore the egress IP after boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'source /var/lib/lab/hooks.sh && lab_ensure_egress'

[Install]
WantedBy=multi-user.target
EOT
systemctl daemon-reload
systemctl enable lab-egress.service

touch /var/lib/lab/ready
echo "node $NODE_INDEX ready"
