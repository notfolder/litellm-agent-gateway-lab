"""Zabbix の監視の一揃い（変更設計書 .history/2026-10-31-Zabbixの監視の一揃い/）。

ホスト litellm-lab-k8s（クラスタの中の zabbix-proxy が集める）に、テンプレートで足りない監視を足す:
ノードの数と逼迫、要求の比、Pending の Pod、OOM、LiteLLM の自動増減の上限、コンテナごとのメモリと CPU の上限比、
PostgreSQL（CloudNativePG）、証明書（cert-manager）、監査ログの整形（CronJob）。あわせて「Zabbix server」ホストの直しと、
ダッシュボード「litellm-lab」。

宣言（`k8s_specs`）を `ensure_specs` が API で作る。差の判定は説明欄に入れた仕様のハッシュ（`lab-spec:<hash>`）で、
同じなら何もしない（冪等）。純粋な関数（宣言を組む部分）は単体テストの対象。
"""

from __future__ import annotations

import hashlib
import json

from .core import LabError, info, ok

HOST = "litellm-lab-k8s"
SPEC_MARK = "lab-spec:"
KSM_MASTER_KEY = "kube.state.metrics"     # テンプレート「Kubernetes cluster state by HTTP」のスクリプトアイテム
DASHBOARD = "litellm-lab"
DASHBOARD_HASH_MACRO = "{$LAB.DASHBOARD.HASH}"
RETIRED_TRIGGERS = ["litellm-lab: Ready のノードが 3 台未満"]
ZABBIX_SERVER_HOST = "Zabbix server"
ZABBIX_SERVER_DROP_TEMPLATE = "Linux by Zabbix agent"

# 前処理の種類（Zabbix 7.0 の API の番号）
PP_JSONPATH = 12
PP_JS = 21
PP_PROMETHEUS = 22
# エラーのときの扱い: 0 = 既定（アイテムを取得できない扱い）、2 = 値を入れる
ON_FAIL_SET = 2
# アイテムの種類と値の型
T_DEPENDENT, T_HTTP, T_CALC = 18, 19, 15
V_FLOAT, V_TEXT = 0, 4
# 深刻度
INFO, WARN, AVG, HIGH = 1, 2, 3, 4

API = "{$KUBE.API.URL}"
BEARER = [{"name": "Authorization", "value": "Bearer {$KUBE.API.TOKEN}"}]


def _hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def prom(pattern: str, how: str = "sum", on_fail: str | None = None) -> list[dict]:
    """Prometheus のパターンの前処理。how が value なら 1 つの値、sum などなら集計。"""
    params = f"{pattern}\nvalue\n" if how == "value" else f"{pattern}\nfunction\n{how}"
    step = {"type": PP_PROMETHEUS, "params": params, "error_handler": 0, "error_handler_params": ""}
    if on_fail is not None:
        step.update({"error_handler": ON_FAIL_SET, "error_handler_params": on_fail})
    return [step]


def js(code: str, on_fail: str | None = None) -> list[dict]:
    step = {"type": PP_JS, "params": code, "error_handler": 0, "error_handler_params": ""}
    if on_fail is not None:
        step.update({"error_handler": ON_FAIL_SET, "error_handler_params": on_fail})
    return [step]


def jsonpath(path: str, on_fail: str | None = None) -> list[dict]:
    step = {"type": PP_JSONPATH, "params": path, "error_handler": 0, "error_handler_params": ""}
    if on_fail is not None:
        step.update({"error_handler": ON_FAIL_SET, "error_handler_params": on_fail})
    return [step]


def dep(key: str, name: str, master: str, preprocessing: list[dict], units: str = "", value_type: int = V_FLOAT) -> dict:
    """従属アイテム（master はキー。ensure のときに itemid に直す）。"""
    return {"key_": key, "name": name, "type": T_DEPENDENT, "master": master, "value_type": value_type,
            "units": units, "preprocessing": preprocessing}


def http(key: str, name: str, path: str, delay: str = "1m") -> dict:
    """Kubernetes の API を読む HTTP アイテム（本文は従属アイテムが読むので履歴は残さない）。"""
    return {"key_": key, "name": name, "type": T_HTTP, "url": API + path, "headers": BEARER, "verify_peer": 0,
            "verify_host": 0, "delay": delay, "timeout": "15s", "value_type": V_TEXT, "history": "0"}


def calc(key: str, name: str, formula: str, units: str = "", delay: str = "1m") -> dict:
    return {"key_": key, "name": name, "type": T_CALC, "params": formula, "delay": delay, "value_type": V_FLOAT, "units": units}


def trig(description: str, expression: str, priority: int) -> dict:
    return {"description": description, "expression": expression, "priority": priority}


# ---- JavaScript（Zabbix の Duktape。ES5） ----
JS_ISO_TO_EPOCH = "return Math.floor(Date.parse(value) / 1000);"
JS_CNPG_ARCHIVING = """var c = (JSON.parse(value).status || {}).conditions || [];
for (var i = 0; i < c.length; i++) { if (c[i].type === 'ContinuousArchiving') { return c[i].status === 'True' ? 1 : 0; } }
return -1;"""
JS_CNPG_PHASE_OK = "return (JSON.parse(value).status || {}).phase === 'Cluster in healthy state' ? 1 : 0;"
JS_CNPG_CREATED = "return Math.floor(Date.parse(JSON.parse(value).metadata.creationTimestamp) / 1000);"
JS_CNPG_LAST_BACKUP = """var items = JSON.parse(value).items || [], last = 0;
for (var i = 0; i < items.length; i++) {
  var s = items[i].status || {};
  if (s.phase === 'completed' && s.stoppedAt) { var t = Math.floor(Date.parse(s.stoppedAt) / 1000); if (t > last) { last = t; } }
}
return last;"""
JS_CERT_READY = """var c = (JSON.parse(value).status || {}).conditions || [];
for (var i = 0; i < c.length; i++) { if (c[i].type === 'Ready') { return c[i].status === 'True' ? 1 : 0; } }
return 0;"""
JS_CONTAINERS_LLD = """var d = JSON.parse(value), out = [];
(d.items || []).forEach(function (p) {
  (p.containers || []).forEach(function (c) {
    out.push({'{#NS}': p.metadata.namespace, '{#POD}': p.metadata.name, '{#CONTAINER}': c.name});
  });
});
return JSON.stringify(out);"""


def _js_usage(field: str) -> str:
    """Metrics API の 1 コンテナの使用量を数に（メモリはバイト、CPU はコア）。LLD のマクロは前処理の中で展開される。"""
    return """var d = JSON.parse(value);
var units = {'n': 1e-9, 'u': 1e-6, 'm': 1e-3, '': 1, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9, 'Ki': 1024, 'Mi': 1048576, 'Gi': 1073741824};
var items = d.items || [];
for (var i = 0; i < items.length; i++) {
  var p = items[i];
  if (p.metadata.namespace !== '{#NS}' || p.metadata.name !== '{#POD}') { continue; }
  for (var j = 0; j < p.containers.length; j++) {
    if (p.containers[j].name !== '{#CONTAINER}') { continue; }
    var m = /^([0-9.]+)([A-Za-z]*)$/.exec(p.containers[j].usage['""" + field + """']);
    if (!m) { throw 'unknown quantity'; }
    return parseFloat(m[1]) * (units[m[2]] === undefined ? 1 : units[m[2]]);
  }
}
throw 'container not found';"""


def k8s_specs() -> dict:
    """ホスト litellm-lab-k8s に足すもの: items（作る順）、triggers、discovery（発見ルールとプロトタイプ）。"""
    h = f"/{HOST}/"
    items = [
        # ノード
        dep("lab.k8s.nodes.ready", "litellm-lab: Ready のノードの数", KSM_MASTER_KEY,
            prom('kube_node_status_condition{condition="Ready",status="true"}')),
        dep("lab.k8s.nodes.pressure", "litellm-lab: 圧迫のあるノードの数", KSM_MASTER_KEY,
            prom('kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure",status="true"}', on_fail="0")),
        dep("lab.k8s.nodes.unschedulable", "litellm-lab: 置けない（cordon）ノードの数", KSM_MASTER_KEY,
            prom("kube_node_spec_unschedulable", on_fail="0")),
        dep("lab.k8s.cpu.requests", "litellm-lab: CPU の要求の合計", KSM_MASTER_KEY,
            prom('kube_pod_container_resource_requests{resource="cpu"}'), units="cores"),
        dep("lab.k8s.cpu.allocatable", "litellm-lab: CPU の割り当て可能な量", KSM_MASTER_KEY,
            prom('kube_node_status_allocatable{resource="cpu"}'), units="cores"),
        dep("lab.k8s.mem.requests", "litellm-lab: メモリの要求の合計", KSM_MASTER_KEY,
            prom('kube_pod_container_resource_requests{resource="memory"}'), units="B"),
        dep("lab.k8s.mem.allocatable", "litellm-lab: メモリの割り当て可能な量", KSM_MASTER_KEY,
            prom('kube_node_status_allocatable{resource="memory"}'), units="B"),
        # Pod
        dep("lab.k8s.pods.pending", "litellm-lab: Pending の Pod の数", KSM_MASTER_KEY,
            prom('kube_pod_status_phase{phase="Pending"}', on_fail="0")),
        dep("lab.k8s.containers.oomkilled", "litellm-lab: 最後に OOM で落ちたコンテナの数", KSM_MASTER_KEY,
            prom('kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}', on_fail="0")),
        dep("lab.k8s.hpa.litellm.current", "litellm-lab: LiteLLM の Pod の数（自動増減）", KSM_MASTER_KEY,
            prom('kube_horizontalpodautoscaler_status_current_replicas{namespace="litellm-lab",horizontalpodautoscaler="litellm"}', "value")),
        dep("lab.k8s.hpa.litellm.max", "litellm-lab: LiteLLM の Pod の上限（自動増減）", KSM_MASTER_KEY,
            prom('kube_horizontalpodautoscaler_spec_max_replicas{namespace="litellm-lab",horizontalpodautoscaler="litellm"}', "value")),
        # 監査ログの整形
        dep("lab.k8s.jobs.failed", "litellm-lab: 失敗した Job の数（litellm-lab）", KSM_MASTER_KEY,
            prom('kube_job_failed{namespace="litellm-lab",condition="true"}', on_fail="0")),
        # 一度も成功していない間は値が無い（kube-state-metrics が出さない）ので 0 にし、作成の時刻と組み合わせて判定する（2026-10-31）
        dep("lab.k8s.compactor.last_success", "litellm-lab: 監査ログの整形の最後の成功", KSM_MASTER_KEY,
            prom('kube_cronjob_status_last_successful_time{namespace="litellm-lab",cronjob="audit-compactor"}', "value", on_fail="0"),
            units="unixtime"),
        dep("lab.k8s.compactor.created", "litellm-lab: 監査ログの整形の作成時刻", KSM_MASTER_KEY,
            prom('kube_cronjob_created{namespace="litellm-lab",cronjob="audit-compactor"}', "value"), units="unixtime"),
        # Kubernetes の API を読む HTTP アイテム（Metrics API、CloudNativePG、cert-manager）
        http("lab.k8s.metrics.pods", "litellm-lab: Metrics API（Pod の使用量）", "/apis/metrics.k8s.io/v1beta1/pods"),
        http("lab.cnpg.cluster", "litellm-lab: PostgreSQL の Cluster", "/apis/postgresql.cnpg.io/v1/namespaces/litellm-lab/clusters/litellm-pg"),
        http("lab.cnpg.backups", "litellm-lab: PostgreSQL の Backup の一覧", "/apis/postgresql.cnpg.io/v1/namespaces/litellm-lab/backups",
             delay="5m"),
        http("lab.cert.litellm", "litellm-lab: 証明書（litellm-tls）", "/apis/cert-manager.io/v1/namespaces/litellm-lab/certificates/litellm-tls",
             delay="10m"),
        dep("lab.cnpg.ready_instances", "litellm-lab: PostgreSQL の準備のできた台数", "lab.cnpg.cluster", jsonpath("$.status.readyInstances")),
        dep("lab.cnpg.instances", "litellm-lab: PostgreSQL の台数（指定）", "lab.cnpg.cluster", jsonpath("$.spec.instances")),
        dep("lab.cnpg.phase_ok", "litellm-lab: PostgreSQL の状態が健全か", "lab.cnpg.cluster", js(JS_CNPG_PHASE_OK)),
        dep("lab.cnpg.archiving", "litellm-lab: PostgreSQL の WAL の保管", "lab.cnpg.cluster", js(JS_CNPG_ARCHIVING)),
        dep("lab.cnpg.created", "litellm-lab: PostgreSQL の作成時刻", "lab.cnpg.cluster", js(JS_CNPG_CREATED), units="unixtime"),
        dep("lab.cnpg.last_backup", "litellm-lab: PostgreSQL の最後のバックアップ", "lab.cnpg.backups", js(JS_CNPG_LAST_BACKUP),
            units="unixtime"),
        dep("lab.cert.not_after", "litellm-lab: 証明書の期限", "lab.cert.litellm", jsonpath("$.status.notAfter") + js(JS_ISO_TO_EPOCH),
            units="unixtime"),
        dep("lab.cert.ready", "litellm-lab: 証明書の準備", "lab.cert.litellm", js(JS_CERT_READY)),
        # 計算アイテム
        calc("lab.k8s.cpu.requests.pct", "litellm-lab: CPU の要求の比", f"100*last(/{HOST}/lab.k8s.cpu.requests)/last(/{HOST}/lab.k8s.cpu.allocatable)", "%"),
        calc("lab.k8s.mem.requests.pct", "litellm-lab: メモリの要求の比", f"100*last(/{HOST}/lab.k8s.mem.requests)/last(/{HOST}/lab.k8s.mem.allocatable)", "%"),
        calc("lab.cert.days_left", "litellm-lab: 証明書の残り日数", f"(last(/{HOST}/lab.cert.not_after)-now())/86400", "d", delay="10m"),
        calc("lab.k8s.nodes.expected", "litellm-lab: 決めた台数（今の 3 台 ＋ 足す台）", "{$LAB.NODES.EXPECTED}", delay="5m"),
    ]
    triggers = [
        # 決めた台数（今の 3 台 ＋ 足す台）は、ホストのマクロ（./lab zabbix が書く。変更設計書 2026-10-31-台の増減とLiteLLMのメモリ 5 章）
        trig("litellm-lab: Ready のノードが決めた台数（{$LAB.NODES.EXPECTED}）より少ない",
             f"last({h}lab.k8s.nodes.ready)<{{$LAB.NODES.EXPECTED}}", HIGH),
        trig("litellm-lab: 圧迫（メモリ・ディスク・プロセス）のあるノードがある", f"last({h}lab.k8s.nodes.pressure)>0", WARN),
        trig("litellm-lab: 置けない（cordon）ノードが 30 分続いている", f"min({h}lab.k8s.nodes.unschedulable,30m)>0", INFO),
        trig("litellm-lab: CPU の要求が割り当て可能な量の 90% を超えた", f"min({h}lab.k8s.cpu.requests.pct,10m)>90", WARN),
        trig("litellm-lab: メモリの要求が割り当て可能な量の 90% を超えた", f"min({h}lab.k8s.mem.requests.pct,10m)>90", WARN),
        trig("litellm-lab: Pending の Pod が 5 分続いている", f"min({h}lab.k8s.pods.pending,5m)>0", WARN),
        trig("litellm-lab: OOM で落ちたコンテナがある", f"last({h}lab.k8s.containers.oomkilled)>0", WARN),
        trig("litellm-lab: LiteLLM の自動増減が上限に 15 分張り付いている",
             f"min({h}lab.k8s.hpa.litellm.current,15m)>=last({h}lab.k8s.hpa.litellm.max)", WARN),
        trig("litellm-lab: litellm-lab の Job が失敗した", f"last({h}lab.k8s.jobs.failed)>0", WARN),
        trig("litellm-lab: 監査ログの整形が 2 時間成功していない",
             f"now()-last({h}lab.k8s.compactor.last_success)>7200 and now()-last({h}lab.k8s.compactor.created)>7200", HIGH),
        trig("litellm-lab: PostgreSQL の準備のできた台数が足りない",
             f"last({h}lab.cnpg.ready_instances)<last({h}lab.cnpg.instances)", WARN),
        trig("litellm-lab: PostgreSQL の状態が 10 分健全でない", f"max({h}lab.cnpg.phase_ok,10m)=0", WARN),
        trig("litellm-lab: PostgreSQL の WAL の保管が失敗している", f"last({h}lab.cnpg.archiving)=0", HIGH),
        trig("litellm-lab: PostgreSQL のバックアップが 26 時間無い",
             f"now()-last({h}lab.cnpg.last_backup)>93600 and now()-last({h}lab.cnpg.created)>93600", HIGH),
        trig("litellm-lab: 証明書の期限まで 14 日を切った", f"last({h}lab.cert.days_left)<14", WARN),
        trig("litellm-lab: 証明書が 1 時間準備できていない", f"max({h}lab.cert.ready,1h)=0", HIGH),
    ]
    key = "[{#NS},{#POD},{#CONTAINER}]"
    name = "{#NS}/{#POD} {#CONTAINER}"
    label = 'namespace="{#NS}",pod="{#POD}",container="{#CONTAINER}"'
    discovery = {
        "rule": {"key_": "lab.k8s.containers.discovery", "name": "litellm-lab: コンテナの発見（Metrics API）", "type": T_DEPENDENT,
                 "master": "lab.k8s.metrics.pods", "lifetime": "1h", "preprocessing": js(JS_CONTAINERS_LLD)},
        "items": [
            dep(f"lab.pod.mem.usage{key}", f"{name}: メモリの使用量", "lab.k8s.metrics.pods", js(_js_usage("memory")), units="B"),
            dep(f"lab.pod.cpu.usage{key}", f"{name}: CPU の使用量", "lab.k8s.metrics.pods", js(_js_usage("cpu")), units="cores"),
            dep(f"lab.pod.mem.limit{key}", f"{name}: メモリの上限", KSM_MASTER_KEY,
                prom(f'kube_pod_container_resource_limits{{{label},resource="memory"}}', "value", on_fail="-1"), units="B"),
            dep(f"lab.pod.cpu.limit{key}", f"{name}: CPU の上限", KSM_MASTER_KEY,
                prom(f'kube_pod_container_resource_limits{{{label},resource="cpu"}}', "value", on_fail="-1"), units="cores"),
            calc(f"lab.pod.mem.pct{key}", f"{name}: メモリ（上限比）",
                 f"100*last(/{HOST}/lab.pod.mem.usage{key})/last(/{HOST}/lab.pod.mem.limit{key})", "%"),
            calc(f"lab.pod.cpu.pct{key}", f"{name}: CPU（上限比）",
                 f"100*last(/{HOST}/lab.pod.cpu.usage{key})/last(/{HOST}/lab.pod.cpu.limit{key})", "%"),
        ],
        "triggers": [
            trig(f"litellm-lab: {name} のメモリが上限の 90% を超えた", f"min({h}lab.pod.mem.pct{key},5m)>90", WARN),
            trig(f"litellm-lab: {name} の CPU が上限の 90% を 15 分超えた", f"avg({h}lab.pod.cpu.pct{key},15m)>90", WARN),
        ],
    }
    return {"items": items, "triggers": triggers, "discovery": discovery}


# ---- API で作る（冪等） ----
def _item_payload(spec: dict, ids: dict[str, str]) -> dict:
    """宣言 → API の本文（master のキーを itemid に）。"""
    body = {k: v for k, v in spec.items() if k != "master"}
    if "master" in spec:
        if spec["master"] not in ids:
            raise LabError(f"従属アイテムの元 {spec['master']} が {HOST} にありません")
        body["master_itemid"] = ids[spec["master"]]
    return body


def _ensure(client, obj: str, parent: dict, find: dict, body: dict, mark_field: str) -> tuple[str, bool]:
    """1 つの物を作るか直す。parent は {hostids|discoveryids: id}（get の絞り込み）。戻り値は (id, 変えたか)。"""
    idfield = {"item": "itemid", "itemprototype": "itemid", "discoveryrule": "itemid", "trigger": "triggerid",
               "triggerprototype": "triggerid"}[obj]
    mark = SPEC_MARK + _hash(body)
    found = client.call(f"{obj}.get", {**parent, "filter": find, "output": [idfield, mark_field]})
    if found and found[0].get(mark_field) == mark:
        return found[0][idfield], False
    payload = {**body, mark_field: mark}
    if found:
        # 更新では親（hostid、ruleid）を渡せない
        payload = {k: v for k, v in payload.items() if k not in ("hostid", "ruleid")}
        client.call(f"{obj}.update", {**payload, idfield: found[0][idfield]})
        return found[0][idfield], True
    created = client.call(f"{obj}.create", payload)
    return created[f"{idfield}s"][0], True


def ensure_specs(client, specs: dict | None = None) -> dict[str, int]:
    """ホスト litellm-lab-k8s に宣言どおりのアイテム・トリガー・発見ルールを作る。作り直した数を返す。"""
    specs = specs or k8s_specs()
    host = client.call("host.get", {"filter": {"host": HOST}, "output": ["hostid"]})
    if not host:
        raise LabError(f"Zabbix のホスト {HOST} がありません（先に Kubernetes のホストを作る）")
    hostid = host[0]["hostid"]
    ids = {i["key_"]: i["itemid"] for i in client.call("item.get", {"hostids": hostid, "output": ["itemid", "key_"]})}
    changed = {"items": 0, "triggers": 0, "discovery": 0}
    for spec in specs["items"]:
        body = {**_item_payload(spec, ids), "hostid": hostid}
        itemid, did = _ensure(client, "item", {"hostids": hostid}, {"key_": spec["key_"]}, body, "description")
        ids[spec["key_"]] = itemid
        changed["items"] += did
    for spec in specs["triggers"]:
        _, did = _ensure(client, "trigger", {"hostids": hostid}, {"description": spec["description"]}, spec, "comments")
        changed["triggers"] += did
    # 名前を変えたトリガーの古い方を消す（台数のトリガーは 2026-10-31 に「3 台未満」から「決めた台数より少ない」へ）
    for old in RETIRED_TRIGGERS:
        stale = client.call("trigger.get", {"hostids": hostid, "filter": {"description": old}, "output": ["triggerid"]})
        if stale:
            client.call("trigger.delete", [t["triggerid"] for t in stale])
            changed["triggers"] += 1
    d = specs["discovery"]
    rule_body = {**_item_payload(d["rule"], ids), "hostid": hostid}
    ruleid, did = _ensure(client, "discoveryrule", {"hostids": hostid}, {"key_": d["rule"]["key_"]}, rule_body, "description")
    changed["discovery"] += did
    for spec in d["items"]:
        body = {**_item_payload(spec, ids), "hostid": hostid, "ruleid": ruleid}
        _, did = _ensure(client, "itemprototype", {"discoveryids": ruleid}, {"key_": spec["key_"]}, body, "description")
        changed["discovery"] += did
    for spec in d["triggers"]:
        _, did = _ensure(client, "triggerprototype", {"discoveryids": ruleid}, {"description": spec["description"]}, spec, "comments")
        changed["discovery"] += did
    total = sum(changed.values())
    (info if total else ok)(f"Zabbix の追加の監視（{HOST}）: " + (f"作り直し {changed}" if total else "最新です"))
    return changed


def fix_zabbix_server_host(client) -> bool:
    """既定の「Zabbix server」ホストから受け身の agent の OS のテンプレートを外す（コンテナの中に agent は居ない）。"""
    host = client.call("host.get", {"filter": {"host": ZABBIX_SERVER_HOST}, "output": ["hostid"],
                                    "selectParentTemplates": ["templateid", "host"]})
    if not host:
        return False
    drop = [t for t in host[0]["parentTemplates"] if t["host"] == ZABBIX_SERVER_DROP_TEMPLATE]
    if not drop:
        return False
    client.call("host.update", {"hostid": host[0]["hostid"], "templates_clear": [{"templateid": drop[0]["templateid"]}]})
    info(f"Zabbix の「{ZABBIX_SERVER_HOST}」ホストから {ZABBIX_SERVER_DROP_TEMPLATE} を外しました（Zabbix 自身の健全性だけ残す）")
    return True


# ---- ダッシュボード ----
def _f(type_: int, name: str, value) -> dict:
    return {"type": type_, "name": name, "value": value}


def dashboard_spec(groupids: list[str], itemids: dict[str, str]) -> dict:
    """ダッシュボード「litellm-lab」の 1 ページ（幅 72 の格子）。"""
    groups = [_f(2, f"groupids.{i}", g) for i, g in enumerate(groupids)]
    widgets = [
        {"type": "problemsbysv", "name": "深刻度ごとの障害", "x": 0, "y": 0, "width": 72, "height": 3, "fields": list(groups)},
        {"type": "problems", "name": "障害", "x": 0, "y": 3, "width": 72, "height": 6, "fields": list(groups)},
    ]
    values = [("lab.k8s.nodes.ready", "Ready のノード"), ("lab.k8s.nodes.expected", "決めた台数"),
              ("lab.k8s.hpa.litellm.max", "LiteLLM の上限"), ("lab.k8s.pods.pending", "Pending の Pod"),
              ("lab.cnpg.ready_instances", "PostgreSQL の台数"), ("lab.cert.days_left", "証明書の残り日数"),
              ("lab.k8s.cpu.requests.pct", "CPU の要求の比"), ("lab.k8s.mem.requests.pct", "メモリの要求の比")]
    for n, (key, title) in enumerate(values):
        if key in itemids:
            widgets.append({"type": "item", "name": title, "x": 9 * n, "y": 9, "width": 9, "height": 3,
                            "fields": [_f(4, "itemid.0", itemids[key])]})
    graphs = [("VM の CPU 使用率", "CPU utilization"), ("VM のメモリ使用率", "Memory utilization"),
              ("VM のディスク（/）の使用率", "FS [/]: Space: Used, in %")]
    for n, (title, pattern) in enumerate(graphs):
        widgets.append({"type": "svggraph", "name": title, "x": 24 * n, "y": 12, "width": 24, "height": 6, "fields": [
            _f(1, "ds.0.hosts.0", "litellm-lab-node*"), _f(1, "ds.0.hosts.1", "litellm-lab-worker*"),
            _f(1, "ds.0.hosts.2", "litellm-lab-proxy"), _f(1, "ds.0.items.0", pattern)]})
    widgets.append({"type": "svggraph", "name": "コンテナのメモリ（上限比）", "x": 0, "y": 18, "width": 72, "height": 6, "fields": [
        _f(1, "ds.0.hosts.0", HOST), _f(1, "ds.0.items.0", "*メモリ（上限比）")]})
    return {"name": DASHBOARD, "display_period": 30, "auto_start": 1, "pages": [{"name": "概要", "widgets": widgets}]}


def ensure_dashboard(client, groupids: list[str]) -> bool:
    host = client.call("host.get", {"filter": {"host": HOST}, "output": ["hostid"]})
    itemids = {}
    if host:
        itemids = {i["key_"]: i["itemid"] for i in client.call("item.get", {"hostids": host[0]["hostid"], "output": ["itemid", "key_"]})}
    spec = dashboard_spec(groupids, itemids)
    digest = _hash(spec)
    macro = client.call("usermacro.get", {"globalmacro": True, "filter": {"macro": DASHBOARD_HASH_MACRO}, "output": ["globalmacroid", "value"]})
    found = client.call("dashboard.get", {"filter": {"name": DASHBOARD}, "output": ["dashboardid"]})
    if found and macro and macro[0]["value"] == digest:
        ok(f"Zabbix のダッシュボード「{DASHBOARD}」は最新です")
        return False
    if found:
        client.call("dashboard.update", {"dashboardid": found[0]["dashboardid"], **spec})
    else:
        client.call("dashboard.create", spec)
    if macro:
        client.call("usermacro.updateglobal", {"globalmacroid": macro[0]["globalmacroid"], "value": digest})
    else:
        client.call("usermacro.createglobal", {"macro": DASHBOARD_HASH_MACRO, "value": digest,
                                               "description": "litellm-lab のダッシュボードの宣言のハッシュ（./lab zabbix が使う）"})
    info(f"Zabbix のダッシュボード「{DASHBOARD}」を{'直しました' if found else '作りました'}")
    return True
