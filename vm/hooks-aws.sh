# AWS 固有の手順（vm/node-init.sh の HOOKS の印に差し込む。labtool/clouds/aws_ec2.py が @@名前@@ を埋める）。
# 関数 5 つ: lab_install_tools、lab_self_id、lab_take_entry_ip、lab_ensure_egress、lab_hook_values。
LAB_REGION="@@REGION@@"
LAB_EIP_ALLOCATION_ID="@@EIP_ALLOCATION_ID@@"
LAB_SECONDARY_IP="@@SECONDARY_IP@@"          # Elastic IP を付ける 2 つ目のプライベート IP
LAB_SUBNET_PREFIX="@@SUBNET_PREFIX@@"        # サブネットのプレフィックス長（2 つ目の IP を付けるとき）

lab_hook_values() {
  echo "LAB_REGION='${LAB_REGION}'"
  echo "LAB_EIP_ALLOCATION_ID='${LAB_EIP_ALLOCATION_ID}'"
  echo "LAB_SECONDARY_IP='${LAB_SECONDARY_IP}'"
}

lab_install_tools() {
  # Ubuntu 24.04 の apt には awscli が無いので、AWS が公開する snap を使う（インスタンスロールで動く）。中継があれば snapd にも伝える
  if [ -n "${HTTPS_PROXY:-}" ]; then
    snap set system proxy.http="${HTTP_PROXY}" proxy.https="${HTTPS_PROXY}" || true
  fi
  command -v aws >/dev/null 2>&1 || snap install aws-cli --classic
  # 2 つ目のプライベート IP は Ubuntu では自動で付かないので、netplan に足す（Elastic IP の宛先）
  local iface
  iface="$(ip -o -4 route show to default | awk '{print $5}' | head -1)"
  cat > /etc/netplan/60-lab-secondary.yaml <<EOT
network:
  version: 2
  ethernets:
    ${iface}:
      addresses:
        - ${LAB_SECONDARY_IP}/${LAB_SUBNET_PREFIX}
EOT
  chmod 600 /etc/netplan/60-lab-secondary.yaml
  netplan apply
}

lab_self_id() {  # 自分のインスタンス ID（IMDSv2。中継を通さない）
  local token
  token="$(curl -s --noproxy '*' -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
  curl -s --noproxy '*' -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id
}

lab_take_entry_ip() {  # 主系になったとき: Elastic IP を自分の 2 つ目のプライベート IP に付け替える（1 つ目のパブリック IP は残る）
  local self i
  self="$(lab_self_id)"
  for i in 1 2 3 4 5 6; do
    if aws ec2 associate-address --region "$LAB_REGION" --allocation-id "$LAB_EIP_ALLOCATION_ID" \
         --instance-id "$self" --private-ip-address "$LAB_SECONDARY_IP" --allow-reassociation; then
      logger -t lab-entry-ip "took ${LAB_EIP_ALLOCATION_ID} on ${self} (${LAB_SECONDARY_IP})"
      return 0
    fi
    sleep 5
  done
  logger -t lab-entry-ip "failed to take ${LAB_EIP_ALLOCATION_ID}"
  return 1
}

lab_ensure_egress() {  # AWS は 1 つ目のパブリック IP が残るので何もしない
  return 0
}
