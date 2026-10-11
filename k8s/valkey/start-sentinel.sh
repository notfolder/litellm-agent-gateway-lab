#!/bin/sh
set -eu
NS=litellm-lab
ME="$(hostname).valkey-sentinel-headless.$NS.svc.cluster.local"
CONF=/conf/sentinel.conf
if [ ! -f "$CONF" ]; then
  PRIMARY=""
  for s in 0 1 2; do
    host="valkey-sentinel-$s.valkey-sentinel-headless.$NS.svc.cluster.local"
    [ "$host" = "$ME" ] && continue
    ans=$(valkey-cli -h "$host" -p 26379 -a "$SENTINEL_PASSWORD" --no-auth-warning \
            SENTINEL get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
    if [ -n "$ans" ]; then PRIMARY="$ans"; break; fi
  done
  [ -n "$PRIMARY" ] || PRIMARY="valkey-0.valkey-headless.$NS.svc.cluster.local"
  # 見張る primary の名前が引けるまで待つ（Sentinel は名前が引けないと監視を始められない）
  until getent hosts "$PRIMARY" >/dev/null 2>&1; do sleep 1; done
  cat > "$CONF" <<EOF
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
EOF
  echo "new sentinel config, watching $PRIMARY"
fi
exec valkey-sentinel "$CONF"
