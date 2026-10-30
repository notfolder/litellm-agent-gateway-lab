#!/bin/bash
# 中継（出口プロキシ。Zscaler 相当）の VM の初期化（labtool/vm.py が SSH で流す。変更設計書 7.1 節）。Ubuntu 24.04 amd64。
# mitmproxy を明示プロキシ（regular）で動かし、独自 CA で TLS を終端し直す。通した通信は /var/log/lab-proxy/flows に残す。
set -euo pipefail
exec > >(tee -a /var/log/lab-proxy-init.log) 2>&1
PORT="@@PORT@@"

if [ ! -f /var/lib/lab/proxy-ready ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q mitmproxy
  mkdir -p /etc/mitmproxy /var/log/lab-proxy /var/lib/lab
  cat > /etc/systemd/system/lab-proxy.service <<EOT
[Unit]
Description=litellm-lab: egress proxy (mitmproxy, explicit mode, private CA)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/mitmdump --mode regular --listen-host 0.0.0.0 --listen-port ${PORT} --set block_global=false \\
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
  touch /var/lib/lab/proxy-ready
fi
systemctl is-active --quiet lab-proxy.service || systemctl restart lab-proxy.service
echo "proxy ready"
