#!/bin/bash
# 中継（出口プロキシ。Zscaler 相当）の VM の初期化（labtool/vm.py が SSH で流す。変更設計書 2026-10-30 7.1 節、2026-10-31 3.4 節）。
# Ubuntu 24.04 amd64。検証ではこの VM が踏み台（管理系の経路の唯一の入口）と Zabbix サーバーも兼ねる。
# mitmproxy を明示プロキシ（regular）で動かし、独自 CA で TLS を終端し直す。通した通信は /var/log/lab-proxy/flows に残す。
set -euo pipefail
exec > >(tee -a /var/log/lab-proxy-init.log) 2>&1
PORT="@@PORT@@"
VPC_CIDR="@@VPC_CIDR@@"            # 中継（3128）と Zabbix（10051）を受ける範囲
ADMIN_CIDRS="@@ADMIN_CIDRS@@"      # 22 を受ける送信元（操作者。カンマ区切り）
ZABBIX="@@ZABBIX@@"                # true なら Zabbix サーバー（Docker Compose）を動かす
ZABBIX_WEB_PORT="@@ZABBIX_WEB_PORT@@"   # web は 127.0.0.1 だけに公開（Mac は SSH の転送で開く）
ZABBIX_RELEASE_URL="@@ZABBIX_RELEASE_URL@@"
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/lab

# ---- mitmproxy ----
# Ubuntu 24.04 の apt の mitmproxy（10.x）が作る証明書には Authority Key Identifier が無く、Python 3.13 以降の既定の厳格な検証
# （VERIFY_X509_STRICT）が拒む（配備で判明 2026-10-31）。PyPI の新しい版を venv に入れる
MARK=/var/lib/lab/proxy-ready-pypi
if [ ! -f "$MARK" ]; then
  systemctl disable --now lab-proxy.service >/dev/null 2>&1 || true
  apt-get update -q
  apt-get install -y -q python3-venv python3-pip
  python3 -m venv /opt/mitmproxy
  /opt/mitmproxy/bin/pip install -q --upgrade pip
  /opt/mitmproxy/bin/pip install -q "mitmproxy>=11"
  rm -rf /etc/mitmproxy
  mkdir -p /etc/mitmproxy /var/log/lab-proxy
  cat > /etc/systemd/system/lab-proxy.service <<EOT
[Unit]
Description=litellm-lab: egress proxy (mitmproxy, explicit mode, private CA)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/mitmproxy/bin/mitmdump --mode regular --listen-host 0.0.0.0 --listen-port ${PORT} --set block_global=false \\
  --set confdir=/etc/mitmproxy --set termlog_verbosity=warn -w /var/log/lab-proxy/flows
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOT
  systemctl daemon-reload
  systemctl enable --now lab-proxy.service
  # 初回起動で CA が生成されるのを待つ
  for _ in $(seq 1 60); do
    [ -s /etc/mitmproxy/mitmproxy-ca-cert.pem ] && break
    sleep 1
  done
  [ -s /etc/mitmproxy/mitmproxy-ca-cert.pem ] || { echo "the proxy ca was not generated"; exit 1; }
  touch "$MARK"
fi
systemctl is-active --quiet lab-proxy.service || systemctl restart lab-proxy.service

# ---- nftables（踏み台の守り）: 22 と 3128 は操作者と VPC の中、10051 は VPC の中、Zabbix の web は 127.0.0.1 だけ。外向きは全部通す ----
set_elems() { local v="${1//,/, }"; [ -n "$v" ] && echo "$v" || echo "127.0.0.255/32"; }
apt-get install -y -q nftables >/dev/null 2>&1 || { apt-get update -q && apt-get install -y -q nftables; }
cat > /etc/nftables.conf <<EOT
#!/usr/sbin/nft -f
# litellm-lab（vm/proxy-init.sh が書く。手で編集しない）
add table inet lab
delete table inet lab
table inet lab {
  set admins   { type ipv4_addr; flags interval; elements = { $(set_elems "$ADMIN_CIDRS") } }
  set internal { type ipv4_addr; flags interval; elements = { $(set_elems "$VPC_CIDR") } }
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ct state invalid drop
    ip protocol icmp accept
    udp sport 67 udp dport 68 accept
    tcp dport 22 ip saddr @admins accept
    tcp dport 22 ip saddr @internal accept
    tcp dport { ${PORT}, 10051 } ip saddr @internal accept
    tcp dport ${PORT} ip saddr @admins accept    # 操作者の Mac も中継を通す（LAB_EGRESS_PROXY_MAC）
    counter drop
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
  }
}
EOT
nft -c -f /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1 || true
nft -f /etc/nftables.conf
echo "nftables applied"

# ---- Zabbix サーバー（Docker Compose: PostgreSQL、server、web）。検証用。本番は既存のサーバーを使う ----
if [ "$ZABBIX" = "true" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q docker.io docker-compose-v2
    systemctl enable --now docker
  fi
  mkdir -p /opt/lab-zabbix
  if [ ! -f /opt/lab-zabbix/.env ]; then
    echo "POSTGRES_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" > /opt/lab-zabbix/.env
    chmod 600 /opt/lab-zabbix/.env
  fi
  cat > /opt/lab-zabbix/compose.yaml <<EOT
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: zabbix
    volumes:
      - db:/var/lib/postgresql/data
    restart: unless-stopped
  server:
    image: zabbix/zabbix-server-pgsql:ubuntu-7.0-latest
    environment:
      DB_SERVER_HOST: db
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: zabbix
      ZBX_CACHESIZE: 64M
    ports:
      - "10051:10051"
    depends_on: [db]
    restart: unless-stopped
  web:
    image: zabbix/zabbix-web-nginx-pgsql:ubuntu-7.0-latest
    environment:
      DB_SERVER_HOST: db
      POSTGRES_USER: zabbix
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: zabbix
      ZBX_SERVER_HOST: server
      PHP_TZ: Asia/Tokyo
    ports:
      - "127.0.0.1:${ZABBIX_WEB_PORT}:8080"
    depends_on: [server]
    restart: unless-stopped
volumes:
  db: {}
EOT
  (cd /opt/lab-zabbix && docker compose up -d --quiet-pull 2>&1 | tail -n 3)
  # agent 2（この台自身の監視）
  if ! command -v zabbix_agent2 >/dev/null 2>&1; then
    curl -sfL -o /tmp/zabbix-release.deb "$ZABBIX_RELEASE_URL" && dpkg -i /tmp/zabbix-release.deb >/dev/null && rm -f /tmp/zabbix-release.deb
    apt-get update -q && apt-get install -y -q zabbix-agent2
  fi
  sed -i -e "s/^Server=.*/Server=127.0.0.1,172.16.0.0\/12/" -e "s/^ServerActive=.*/ServerActive=127.0.0.1:10051/" \
         -e "s/^Hostname=.*/Hostname=$(hostname)/" -e "s/^# HostMetadata=.*/HostMetadata=litellm-lab-bastion/" \
         -e "s/^HostMetadata=.*/HostMetadata=litellm-lab-bastion/" /etc/zabbix/zabbix_agent2.conf
  grep -q "^HostMetadata=" /etc/zabbix/zabbix_agent2.conf || echo "HostMetadata=litellm-lab-bastion" >> /etc/zabbix/zabbix_agent2.conf
  systemctl enable zabbix-agent2 >/dev/null 2>&1 || true
  systemctl restart zabbix-agent2
  echo "zabbix server up (web on 127.0.0.1:${ZABBIX_WEB_PORT})"
fi
echo "proxy ready"
