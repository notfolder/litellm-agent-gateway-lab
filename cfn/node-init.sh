#!/bin/bash
# EC2 ノードの初期化（cloud-init の user data。LAB_TARGET=aws-ec2。変更設計書 .history/2026-10-25-EC2三台構成/ 3.4〜3.5 節）。
# labtool/cfn.py が @@名前@@ を置き換えてから CloudFormation に渡す。3 台で同じ内容で、台ごとの値だけが違う。
# 秘密（k3s の参加トークン）はここに書かず、Systems Manager のパラメータから取る。
set -euo pipefail
exec > >(tee -a /var/log/lab-node-init.log) 2>&1

REGION="@@REGION@@"
NODE_INDEX="@@NODE_INDEX@@"            # 1〜3
SELF_IP="@@SELF_IP@@"                  # 自分のプライベート IP（固定）
FIRST_IP="@@FIRST_IP@@"                # 1 番の台のプライベート IP
PEER_IPS="@@PEER_IPS@@"                # 他の 2 台のプライベート IP（空白区切り）
SECONDARY_IP="@@SECONDARY_IP@@"        # Elastic IP を付ける 2 つ目のプライベート IP
VERSION_PARAM="@@VERSION_PARAM@@"      # k3s の版を置く SSM のパラメータ名（例の値 v1.35.5+k3s1。版上げ後は新しい版）
TOKEN_PARAM="@@TOKEN_PARAM@@"          # SSM のパラメータ名（SecureString）
EIP_ALLOCATION_ID="@@EIP_ALLOCATION_ID@@"
PRIORITY="@@PRIORITY@@"                # keepalived の優先度（1 番が高い）
REGISTRY_HOST="@@REGISTRY_HOST@@"      # クラスタの中のイメージ置き場の名前（例 lab-registry:5000）
REGISTRY_NODEPORT="@@REGISTRY_NODEPORT@@"

if [ -f /var/lib/lab/ready ]; then
  echo "already initialized"; exit 0
fi
mkdir -p /var/lib/lab /etc/rancher/k3s /etc/keepalived /usr/local/bin

# ---- 道具 ----
dnf install -y keepalived

# ---- イメージ置き場（クラスタの中の registry）を、各ノードが自分の NodePort 経由で引く ----
# 名前は解決できなくても、containerd はミラーの endpoint に向かう。/etc/hosts にも足して確実にする
cat > /etc/rancher/k3s/registries.yaml <<EOF
mirrors:
  "${REGISTRY_HOST}":
    endpoint:
      - "http://${SELF_IP}:${REGISTRY_NODEPORT}"
EOF
REGISTRY_NAME="${REGISTRY_HOST%%:*}"
grep -q " ${REGISTRY_NAME}\$" /etc/hosts || echo "127.0.0.1 ${REGISTRY_NAME}" >> /etc/hosts

# ---- k3s ----
# 版は SSM のパラメータから（user data に書かない。./lab upgrade-k8s が上げた後に作り直した台も、同じ版で参加する）
K3S_VERSION="$(aws ssm get-parameter --region "$REGION" --name "$VERSION_PARAM" \
                --query Parameter.Value --output text)"
TOKEN="$(aws ssm get-parameter --region "$REGION" --name "$TOKEN_PARAM" --with-decryption \
          --query Parameter.Value --output text)"

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

# ---- keepalived: Elastic IP の持ち主を 3 台の多数決で決める（ユニキャスト VRRP） ----
IFACE="$(ip -o -4 route show to default | awk '{print $5}' | head -1)"

cat > /usr/local/bin/lab-health.sh <<'EOF'
#!/bin/bash
# この台が入口として健全か: k3s が動き、Traefik（80）が応答する
systemctl is-active --quiet k3s || exit 1
curl -s -o /dev/null --max-time 2 http://127.0.0.1:80/ || exit 1
exit 0
EOF

cat > /usr/local/bin/lab-take-eip.sh <<EOF
#!/bin/bash
# 主系になったとき: Elastic IP を自分の 2 つ目のプライベート IP に付け替える（1 つ目のパブリック IP は残る）
IMDS_TOKEN="\$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
INSTANCE_ID="\$(curl -s -H "X-aws-ec2-metadata-token: \$IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)"
for i in 1 2 3 4 5 6; do
  if aws ec2 associate-address --region "${REGION}" --allocation-id "${EIP_ALLOCATION_ID}" \
       --instance-id "\$INSTANCE_ID" --private-ip-address "${SECONDARY_IP}" --allow-reassociation; then
    logger -t lab-eip "took ${EIP_ALLOCATION_ID} on \$INSTANCE_ID (${SECONDARY_IP})"
    exit 0
  fi
  sleep 5
done
logger -t lab-eip "failed to take ${EIP_ALLOCATION_ID}"
exit 1
EOF
chmod 755 /usr/local/bin/lab-health.sh /usr/local/bin/lab-take-eip.sh

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
  # 仮想 IP は使わないリンクローカルのダミー。実際の経路は Elastic IP の付け替えで切り替わる
  virtual_ipaddress {
    169.254.60.1/32 dev ${IFACE}
  }
  track_script {
    chk_lab
  }
  notify_master "/usr/local/bin/lab-take-eip.sh"
}
EOF
systemctl enable --now keepalived

touch /var/lib/lab/ready
echo "node $NODE_INDEX ready"
