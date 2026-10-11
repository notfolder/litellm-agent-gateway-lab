"""障害の試験と、任意の時点への復元の試験（変更仕様書 9.3・9.4 節、変更設計書 10 章）。

- 障害の試験: 偽 LLM（fake-tools）に 1 秒 20 件の要求を送り続けながら、PostgreSQL と Valkey の primary を
  切り替える・消す・載っているノードを止める。失敗した要求の数、失敗が続いた時間、回復までの時間を測る
- 復元の試験: 時刻 T を記録してから行を足し、T を指定して別のクラスタに戻す。T までの行があり、後の行が無いこと
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .core import (
    BUILD_DIR, KUBE_CONTEXT, NS, Env, LabError, info, json_output, kc, kn, ok, output, run, step, wait_until, warn,
)

SERVER_NODE = "k3d-litellm-server-0"
PG_CLUSTER = "litellm-pg"
SCENARIOS = ["pg-switchover", "pg-pod", "pg-node", "valkey-pod", "valkey-node"]
# 回復までの時間の合格の目安（秒）。ノードを止める場面は長め（変更仕様書 9.3 節）
LIMITS = {"pg-switchover": 60, "pg-pod": 60, "pg-node": 120, "valkey-pod": 60, "valkey-node": 120}
# 直結（比較用）のときだけ、PostgreSQL の primary の切り替えと Pod の削除は 90 秒にする。Pooler は切り替えの間の要求を
# 待たせて吸収する（2026-10-12 の実測で、どちらも失敗 0 件）が、直結では新しい primary が立つまでの 15〜50 秒が
# そのまま失敗になり、最後の失敗まで 54〜77 秒だった（変更仕様書 9.3 節の目安を、1 回目の結果を見て見直した）
DIRECT_LIMITS = {"pg-switchover": 90, "pg-pod": 90}
NODE_DOWN_SECONDS = 120


def cnpg(*args: str) -> list[str]:
    """kubectl cnpg（プラグイン）。プラグインには、全体のフラグをプラグイン名の後ろに渡す。"""
    return ["kubectl", "cnpg", "--context", KUBE_CONTEXT, "-n", NS, *args]


# ---- 負荷 ----
@dataclass
class Sample:
    start: float
    status: int
    call_id: str = ""
    pod: str = ""


@dataclass
class Load:
    """1 秒 rate 件の要求を送り続け、要求ごとの開始時刻、結果、x-litellm-call-id を記録する。"""

    env: Env
    rate: int = 20
    samples: list[Sample] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _one(self, start: float) -> None:
        req = urllib.request.Request(
            f"{self.env.base_url}/v1/chat/completions", method="POST",
            data=json.dumps({"model": "fake-tools", "messages": [{"role": "user", "content": "failover"}]}).encode(),
            headers={"Authorization": f"Bearer {self.env.get('LITELLM_MASTER_KEY')}",
                     "content-type": "application/json"})
        status, call_id = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
                status, call_id = resp.status, resp.headers.get("x-litellm-call-id", "")
        except urllib.error.HTTPError as e:
            status, call_id = e.code, e.headers.get("x-litellm-call-id", "") if e.headers else ""
        except Exception:  # noqa: BLE001 接続できない・時間切れは 0 として数える
            status = 0
        with self._lock:
            self.samples.append(Sample(start, status, call_id))

    def run(self) -> None:
        interval = 1.0 / self.rate
        with ThreadPoolExecutor(max_workers=self.rate * 4) as pool:
            nxt = time.monotonic()
            while not self._stop.is_set():
                pool.submit(self._one, time.time())
                nxt += interval
                time.sleep(max(0.0, nxt - time.monotonic()))

    def __enter__(self) -> "Load":
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def failures_after(self, t: float) -> list[Sample]:
        with self._lock:
            return [s for s in self.samples if s.start >= t and s.status != 200]

    def last_failure(self) -> float | None:
        with self._lock:
            fails = [s.start for s in self.samples if s.status != 200]
        return max(fails) if fails else None


# ---- 状態 ----
def pg_primary() -> str:
    return output(kn("get", "cluster", PG_CLUSTER, "-o", "jsonpath={.status.currentPrimary}"), check=False).strip()


def pod_node(pod: str) -> str:
    return output(kn("get", "pod", pod, "-o", "jsonpath={.spec.nodeName}"), check=False).strip()


def valkey_roles(env: Env) -> dict[str, str]:
    roles = {}
    for i in range(3):
        out = output(kn("exec", f"valkey-{i}", "--", "valkey-cli", "-a", env.get("REDIS_PASSWORD"), "--no-auth-warning",
                        "info", "replication"), check=False)
        for line in out.splitlines():
            if line.startswith("role:"):
                roles[f"valkey-{i}"] = line.split(":", 1)[1].strip()
    return roles


def valkey_primary(env: Env) -> str:
    from .stages import sentinel_masters
    masters = sentinel_masters(env)
    return masters[0].split(".")[0] if masters else ""


def litellm_restarts() -> dict[str, int]:
    pods = json_output(kn("get", "pods", "-l", "app.kubernetes.io/name=litellm", "-o", "json"), default={}) or {}
    return {p["metadata"]["name"]: sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
            for p in pods.get("items", [])}


def healthy(env: Env) -> bool:
    from .stages import sentinel_masters
    st = (json_output(kn("get", "cluster", PG_CLUSTER, "-o", "json"), default={}) or {}).get("status") or {}
    if st.get("readyInstances") != 3:
        return False
    masters = sentinel_masters(env)
    if len(masters) != 3 or len(set(masters)) != 1:
        return False
    if list(valkey_roles(env).values()).count("master") != 1:
        return False
    ready = output(kn("get", "statefulset", "valkey", "-o", "jsonpath={.status.readyReplicas}"), check=False).strip()
    return ready == "3"


def wait_healthy(env: Env, timeout: int = 600) -> None:
    wait_until(timeout, "PostgreSQL 3 台と Valkey 3 台がそろい、primary が 1 台", lambda: healthy(env))
    run(kn("rollout", "status", "deploy/litellm", "--timeout=300s"), quiet=True)


# ---- 場面 ----
def _move_pg_primary_off_server() -> None:
    primary = pg_primary()
    if pod_node(primary) != SERVER_NODE:
        return
    pods = output(kn("get", "pods", "-l", f"cnpg.io/cluster={PG_CLUSTER},cnpg.io/podRole=instance", "-o", "name"),
                  check=False).split()
    target = next(p.split("/")[1] for p in pods if pod_node(p.split("/")[1]) != SERVER_NODE and p.split("/")[1] != primary)
    info(f"PostgreSQL の primary（{primary}）が server ノードにあるので、先に {target} へ切り替えます")
    run(cnpg("promote", PG_CLUSTER, target))
    wait_until(180, f"primary が {target} になる", lambda: pg_primary() == target)


def _move_valkey_primary_off_server(env: Env) -> None:
    for _ in range(5):
        primary = valkey_primary(env)
        if pod_node(primary) != SERVER_NODE:
            return
        info(f"Valkey の primary（{primary}）が server ノードにあるので、先に切り替えます")
        run(kn("exec", "valkey-sentinel-0", "--", "valkey-cli", "-p", "26379", "-a", env.get("SENTINEL_PASSWORD"),
               "--no-auth-warning", "SENTINEL", "FAILOVER", "mymaster"), quiet=True)
        time.sleep(15)
        wait_healthy(env)
    raise LabError("Valkey の primary を agent ノードへ移せませんでした")


def _stop_node(node: str, load: Load) -> None:
    if node == SERVER_NODE:
        raise LabError("server ノードは止めません（Kubernetes の制御部分と RustFS が載っている）")
    info(f"ノード {node} を {NODE_DOWN_SECONDS} 秒止めます")
    run(["docker", "stop", node], quiet=True)
    try:
        time.sleep(NODE_DOWN_SECONDS)
    finally:
        run(["docker", "start", node], quiet=True)
        info(f"ノード {node} を起動しました")
    wait_until(300, f"{node} の Ready", kc("wait", "--for=condition=Ready", f"node/{node}", "--timeout=5s"))


def trigger(env: Env, scenario: str, load: Load) -> str:
    """場面を起こす。何をしたかの説明を返す。"""
    if scenario == "pg-switchover":
        primary = pg_primary()
        pods = output(kn("get", "pods", "-l", f"cnpg.io/cluster={PG_CLUSTER},cnpg.io/podRole=instance", "-o", "name"),
                      check=False).split()
        target = next(p.split("/")[1] for p in pods if p.split("/")[1] != primary)
        run(cnpg("promote", PG_CLUSTER, target))
        return f"PostgreSQL の primary を {primary} から {target} へ計画的に切り替え"
    if scenario == "pg-pod":
        primary = pg_primary()
        run(kn("delete", "pod", primary, "--wait=false"))
        return f"PostgreSQL の primary の Pod {primary} を削除"
    if scenario == "pg-node":
        primary = pg_primary()
        node = pod_node(primary)
        _stop_node(node, load)
        return f"PostgreSQL の primary（{primary}）が載ったノード {node} を停止"
    if scenario == "valkey-pod":
        primary = valkey_primary(env)
        run(kn("delete", "pod", primary, "--wait=false"))
        return f"Valkey の primary の Pod {primary} を削除"
    if scenario == "valkey-node":
        primary = valkey_primary(env)
        node = pod_node(primary)
        _stop_node(node, load)
        return f"Valkey の primary（{primary}）が載ったノード {node} を停止"
    raise LabError(f"知らない場面です: {scenario}")


def run_scenario(env: Env, scenario: str) -> dict:
    step(f"test-failover {scenario}")
    wait_healthy(env)
    if scenario == "pg-node":
        _move_pg_primary_off_server()
        wait_healthy(env)
    if scenario == "valkey-node":
        _move_valkey_primary_off_server(env)
    restarts_before = litellm_restarts()
    with Load(env) as load:
        time.sleep(10)  # 平常時の様子
        baseline_fail = len(load.failures_after(0))
        t0 = time.time()
        what = trigger(env, scenario, load)
        info(what)
        # 回復を待つ: 起こしてから最低 60 秒、かつ直近 20 秒に失敗が無い（最長 300 秒）
        deadline = t0 + 300 + (NODE_DOWN_SECONDS if scenario.endswith("-node") else 0)
        while time.time() < deadline:
            time.sleep(2)
            last = load.last_failure()
            if time.time() - t0 >= 60 and (last is None or time.time() - last >= 20):
                break
        samples = list(load.samples)
    wait_healthy(env)
    restarts_after = litellm_restarts()
    fails = [s for s in samples if s.start >= t0 and s.status != 200]
    recovery = (max(s.start for s in fails) - t0) if fails else 0.0
    window = (max(s.start for s in fails) - min(s.start for s in fails)) if fails else 0.0
    repeated = {p: restarts_after[p] - restarts_before.get(p, 0) for p in restarts_after
                if restarts_after[p] - restarts_before.get(p, 0) > 1}
    roles = valkey_roles(env)
    result = {
        "scenario": scenario, "what": what, "requests": len([s for s in samples if s.start >= t0]),
        "failed": len(fails), "baseline_failed": baseline_fail,
        "statuses": sorted({s.status for s in fails}), "recovery_seconds": round(recovery, 1),
        "failure_window_seconds": round(window, 1), "limit_seconds": LIMITS[scenario],
        "litellm_restart_loops": repeated, "valkey_masters": list(roles.values()).count("master"),
        "pg_primary_after": pg_primary(), "valkey_primary_after": valkey_primary(env),
        "call_ids": [s.call_id for s in samples if s.status == 200 and s.call_id],
        "t0": t0,
    }
    limit = (DIRECT_LIMITS.get(scenario, LIMITS[scenario]) if env.get("LAB_DB_POOLER", "pooler") == "direct"
             else LIMITS[scenario])
    result["limit_seconds"] = limit
    result["passed"] = (recovery <= limit and not repeated and result["valkey_masters"] == 1
                        and baseline_fail == 0)
    (ok if result["passed"] else warn)(
        f"{scenario}: 要求 {result['requests']} 件のうち失敗 {result['failed']} 件"
        f"（状態 {result['statuses']}）、回復まで {result['recovery_seconds']} 秒（目安 {limit} 秒以内）、"
        f"失敗が続いた時間 {result['failure_window_seconds']} 秒")
    return result


def failover(env: Env, scenarios: list[str], audit_check=None) -> None:
    """障害の試験。audit_check(results) を渡すと、監査ログの欠けも照合する。"""
    step(f"test-failover 障害の試験（DB の接続先: {env.get('LAB_DB_POOLER', 'pooler')}）")
    results = [run_scenario(env, s) for s in scenarios]
    if audit_check is not None:
        audit_check(results)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = BUILD_DIR / f"failover-{stamp}.json"
    summary = [{k: v for k, v in r.items() if k != "call_ids"} for r in results]
    out.write_text(json.dumps({"db_pooler": env.get("LAB_DB_POOLER", "pooler"), "results": summary},
                              ensure_ascii=False, indent=2) + "\n")
    info(f"結果: {out.relative_to(BUILD_DIR.parent)}")
    failed = [r["scenario"] for r in results if not r["passed"]]
    if failed:
        raise LabError(f"障害の試験で目安を満たさなかった場面: {', '.join(failed)}")
    ok(f"障害の試験: {len(results)} 場面すべて目安を満たしました")


# ---- 復元 ----
def _psql(cluster: str, sql: str) -> str:
    """クラスタの primary の Pod の中で psql を流す（postgres ユーザー。端末を使わない）。"""
    primary = output(kn("get", "cluster", cluster, "-o", "jsonpath={.status.currentPrimary}")).strip()
    return output(kn("exec", primary, "-c", "postgres", "--", "psql", "-d", "litellm", "-At", "-c", sql)).strip()


RESTORE_MANIFEST = """apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: litellm-pg-restore
  namespace: litellm-lab
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:17.11-minimal-trixie
  storage:
    storageClass: local-path
    size: 2Gi
  resources:
    requests: {{ cpu: 100m, memory: 256Mi }}
    limits: {{ memory: 512Mi }}
  bootstrap:
    recovery:
      source: origin
      recoveryTarget:
        targetTime: "{target}"
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: pg-backup
          serverName: litellm-pg
"""


def restore(env: Env) -> None:
    """バックアップと WAL から、時刻 T の状態を別のクラスタ（litellm-pg-restore）に戻して確かめる。"""
    step("test-restore 任意の時点への復元の試験")
    run(kn("delete", "cluster", "litellm-pg-restore", "--ignore-not-found", "--wait=true"), quiet=True)
    # 復元の起点になるバックアップを取る（毎日の定期バックアップを待たない）
    name = f"restore-check-{time.strftime('%Y%m%d%H%M%S')}"
    run(kn("apply", "-f", "-"), input=json.dumps({
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"cluster": {"name": PG_CLUSTER}, "method": "plugin",
                 "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}}}), quiet=True)
    wait_until(600, "バックアップの完了", lambda: output(kn("get", "backup", name, "-o", "jsonpath={.status.phase}"),
                                                      check=False).strip() == "completed")
    _psql(PG_CLUSTER, "CREATE TABLE IF NOT EXISTS lab_restore_check (label text, at timestamptz DEFAULT now())")
    _psql(PG_CLUSTER, "INSERT INTO lab_restore_check (label) VALUES ('before')")
    time.sleep(2)
    target = _psql(PG_CLUSTER, "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
    time.sleep(2)
    _psql(PG_CLUSTER, "INSERT INTO lab_restore_check (label) VALUES ('after')")
    # WAL を切り替えて、T の後の行を含む WAL まで送らせる
    wal = _psql(PG_CLUSTER, "SELECT pg_walfile_name(pg_switch_wal())")
    wait_until(300, f"WAL {wal} の送信", lambda: _psql(
        PG_CLUSTER, "SELECT last_archived_wal FROM pg_stat_archiver") >= wal)
    info(f"時刻 T = {target} を指定して復元します")
    run(kn("apply", "-f", "-"), input=RESTORE_MANIFEST.format(target=target), quiet=True)
    try:
        wait_until(900, "復元したクラスタの準備", kn("wait", "cluster/litellm-pg-restore", "--for=condition=Ready",
                                                   "--timeout=5s"))
        labels = _psql("litellm-pg-restore", "SELECT string_agg(label, ',' ORDER BY at) FROM lab_restore_check")
        info(f"復元したクラスタの行: {labels}")
        if "before" not in labels.split(",") or "after" in labels.split(","):
            raise LabError(f"復元の結果が期待と違います（T までの行 before があり、after が無いはず）: {labels}")
        ok("復元の試験: T までの行があり、T より後の行はありませんでした")
    finally:
        run(kn("delete", "cluster", "litellm-pg-restore", "--ignore-not-found", "--wait=false"), quiet=True)
        run(kn("delete", "backup", name, "--ignore-not-found"), quiet=True, check=False)
        _psql(PG_CLUSTER, "DROP TABLE IF EXISTS lab_restore_check")
