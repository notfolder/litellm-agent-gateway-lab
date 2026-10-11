#!/bin/sh
set -eu
NS=litellm-lab
ME="$(hostname).valkey-headless.$NS.svc.cluster.local"
FIRST="valkey-0.valkey-headless.$NS.svc.cluster.local"
PRIMARY=""
for i in 1 2 3; do
  for s in 0 1 2; do
    ans=$(valkey-cli -h "valkey-sentinel-$s.valkey-sentinel-headless.$NS.svc.cluster.local" -p 26379 \
            -a "$SENTINEL_PASSWORD" --no-auth-warning SENTINEL get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
    if [ -n "$ans" ]; then PRIMARY="$ans"; break 2; fi
  done
  sleep 1
done
if [ -z "$PRIMARY" ]; then
  PRIMARY="$FIRST"   # Sentinel がまだ誰も知らない（最初の起動）
fi
{
  echo "port 6379"
  echo "dir /data"
  echo "appendonly yes"
  echo "appendfsync everysec"
  echo "save 900 1"
  echo "maxmemory 128mb"
  echo "maxmemory-policy noeviction"
  echo "min-replicas-to-write 1"
  echo "min-replicas-max-lag 10"
  echo "requirepass $REDIS_PASSWORD"
  echo "masterauth $REDIS_PASSWORD"
  echo "replica-announce-ip $ME"
  if [ "$PRIMARY" != "$ME" ]; then echo "replicaof $PRIMARY 6379"; fi
} > /tmp/valkey.conf
echo "start as $( [ "$PRIMARY" = "$ME" ] && echo primary || echo "replica of $PRIMARY" )"
exec valkey-server /tmp/valkey.conf
