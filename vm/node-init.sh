#!/bin/bash
# VM ノードの初期化（LAB_TARGET=aws-ec2 は cloud-init の user data、gcp-vm は startup-script。
# 変更設計書 .history/2026-10-30-VM環境の共通化とGCP追加/ 6 章）。Ubuntu 24.04 LTS arm64。
# labtool/vm.py が @@名前@@ を置き換え、クラウド固有の手順（vm/hooks-aws.sh / hooks-gcp.sh）を HOOKS の印に差し込む。
# 3 台で同じ内容で、台ごとの値だけが違う。秘密（k3s の参加トークン）はここに書かず、バケットから自分の権限で読む。
set -euo pipefail
exec > >(tee -a /var/log/lab-node-init.log) 2>&1

NODE_INDEX="@@NODE_INDEX@@"            # 1〜3
SELF_IP="@@SELF_IP@@"                  # 自分のプライベート IP（固定）
FIRST_IP="@@FIRST_IP@@"                # 1 番の台のプライベート IP
PEER_IPS="@@PEER_IPS@@"                # 他の 2 台のプライベート IP（空白区切り）
PRIORITY="@@PRIORITY@@"                # keepalived の優先度（1 番が高い）
REGISTRY_HOST="@@REGISTRY_HOST@@"      # クラスタの中のイメージ置き場の名前（例 lab-registry:5000）
REGISTRY_NODEPORT="@@REGISTRY_NODEPORT@@"
SSH_PUBKEY="@@SSH_PUBKEY@@"            # 構築ツールの SSH の公開鍵

# ---- クラウド固有の手順（関数: lab_install_tools、lab_read_object、lab_self_id、lab_take_entry_ip、lab_ensure_egress、lab_hook_values） ----
@@HOOKS@@
# ---- ここからクラウドを知らない ----

# 構築ツールの SSH の鍵（GCP はメタデータからも入るが、同じ行があっても害は無い）
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
grep -qF "$SSH_PUBKEY" /home/ubuntu/.ssh/authorized_keys 2>/dev/null || echo "$SSH_PUBKEY" >> /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys && chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

if [ -f /var/lib/lab/ready ]; then
  # 2 回目以降の起動（GCP の startup-script は毎回走る）: 外向きの IP だけ確かめて抜ける
  lab_ensure_egress || true
  echo "already initialized"; exit 0
fi
mkdir -p /var/lib/lab /etc/rancher/k3s /etc/keepalived /usr/local/bin

# ---- 道具 ----
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q keepalived curl
lab_install_tools   # hooks: クラウドの API を呼ぶ道具（AWS は aws-cli の snap。GCP は curl で足りる）

# ---- イメージ置き場（クラスタの中の registry）を、各ノードが自分の NodePort 経由で引く ----
cat > /etc/rancher/k3s/registries.yaml <<EOF
mirrors:
  "${REGISTRY_HOST}":
    endpoint:
      - "http://${SELF_IP}:${REGISTRY_NODEPORT}"
EOF
REGISTRY_NAME="${REGISTRY_HOST%%:*}"
grep -q " ${REGISTRY_NAME}\$" /etc/hosts || echo "127.0.0.1 ${REGISTRY_NAME}" >> /etc/hosts

# ---- k3s ----
# 版とトークンはバケットから（user data に書かない。./lab upgrade-k8s が上げた後に作り直した台も、同じ版で参加する）
K3S_VERSION="$(lab_read_object k3s/version | tr -d '[:space:]')"
TOKEN="$(lab_read_object k3s/token | tr -d '[:space:]')"
[ -n "$K3S_VERSION" ] && [ -n "$TOKEN" ] || { echo "k3s version or token is missing in the bucket"; exit 1; }

alive() {  # 6443 が何か答えるか（k3s の server が起きているか）
  local code
  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "https://$1:6443/ping" || true)"
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
# トークンは参加のときだけ環境変数で渡す（server/token に保存され、以後の再起動と版上げでは要らない）
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" K3S_TOKEN="$TOKEN" sh -s - server
unset TOKEN

for _ in $(seq 1 120); do
  if /usr/local/bin/k3s kubectl get --raw /readyz >/dev/null 2>&1; then break; fi
  sleep 5
done

# ---- keepalived: 浮動 IP の持ち主を 3 台の多数決で決める（ユニキャスト VRRP） ----
IFACE="$(ip -o -4 route show to default | awk '{print $5}' | head -1)"

cat > /usr/local/bin/lab-health.sh <<'EOF'
#!/bin/bash
# この台が入口として健全か: k3s が動き、Traefik（80）が応答する
systemctl is-active --quiet k3s || exit 1
curl -s -o /dev/null --max-time 2 http://127.0.0.1:80/ || exit 1
exit 0
EOF

# 主系になったとき（notify_master）と、主系を降りたとき（notify_backup）に呼ぶ。中身はクラウド固有の関数
cat > /usr/local/bin/lab-take-entry-ip.sh <<EOF
#!/bin/bash
source /var/lib/lab/hooks.sh
lab_take_entry_ip
EOF
cat > /usr/local/bin/lab-release-entry-ip.sh <<EOF
#!/bin/bash
source /var/lib/lab/hooks.sh
lab_ensure_egress
EOF
# hooks の関数（補助の関数も含めて全部）と値を、keepalived の通知スクリプトが読む形で残す
declare -f > /var/lib/lab/hooks.sh
lab_hook_values >> /var/lib/lab/hooks.sh
chmod 755 /usr/local/bin/lab-health.sh /usr/local/bin/lab-take-entry-ip.sh /usr/local/bin/lab-release-entry-ip.sh
chmod 600 /var/lib/lab/hooks.sh

PEER_BLOCK=""
for ip in $PEER_IPS; do PEER_BLOCK="${PEER_BLOCK}    ${ip}
"; done
cat > /etc/keepalived/keepalived.conf <<EOF
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
EOF
systemctl enable --now keepalived

touch /var/lib/lab/ready
echo "node $NODE_INDEX ready"
