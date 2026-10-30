#!/bin/bash
# 中継（出口プロキシ。Zscaler 相当）の VM の初期化（labtool/vm.py が SSH で流す。変更設計書 7.1 節）。Ubuntu 24.04 amd64。
# mitmproxy を明示プロキシ（regular）で動かし、独自 CA で TLS を終端し直す。通した通信は /var/log/lab-proxy/flows に残す。
set -euo pipefail
exec > >(tee -a /var/log/lab-proxy-init.log) 2>&1
PORT="@@PORT@@"

# Ubuntu 24.04 の apt の mitmproxy（10.x）が作る証明書には Authority Key Identifier が無く、Python 3.13 以降の既定の厳格な検証
# （VERIFY_X509_STRICT）が拒む（配備で判明 2026-10-31）。PyPI の新しい版を venv に入れる
MARK=/var/lib/lab/proxy-ready-pypi
if [ ! -f "$MARK" ]; then
  export DEBIAN_FRONTEND=noninteractive
  systemctl disable --now lab-proxy.service >/dev/null 2>&1 || true
  apt-get update -q
  apt-get install -y -q python3-venv python3-pip
  python3 -m venv /opt/mitmproxy
  /opt/mitmproxy/bin/pip install -q --upgrade pip
  /opt/mitmproxy/bin/pip install -q "mitmproxy>=11"
  rm -rf /etc/mitmproxy
  mkdir -p /etc/mitmproxy /var/log/lab-proxy /var/lib/lab
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
echo "proxy ready"
