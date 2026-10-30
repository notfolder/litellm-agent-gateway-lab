"""Zabbix による監視（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-31-内部LBとVM側の防御とZabbix/ 7 章）。

- VM（3 台と踏み台）は Zabbix agent 2 の能動チェックで送り、自動登録のアクション（HostMetadata）でホストが作られる。
- k3s は Zabbix の公式 Helm チャート（zabbix-proxy と kube-state-metrics。チャートの agent は入れない: VM の agent 2 と 10050 が
  重なり、ノードの名前も重なる）と、テンプレート「Kubernetes cluster state by HTTP」（kubelet などは LLD が作る）。
- 入口は HTTP agent のアイテム（/health/liveliness、/health/readiness）をクラスタの中の proxy から見る。
- 設定は Zabbix の API（JSON-RPC）で作り、2 回目以降は差が無ければ何もしない（冪等）。
検証（LAB_ZABBIX_VM=true）では踏み台の VM の Zabbix サーバーを使い、初回に Admin のパスワードを変えて API トークンを作る。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import render, vm
from .core import NS, Env, LabError, gen_secret, helm, info, kc, ok, output, run, succeeds, wait_until, warn

CHART_REPO = "https://cdn.zabbix.com/zabbix/integrations/kubernetes-helm/7.0"
CHART_REPO_NAME = "zabbix-chart-7.0"
CHART = "zabbix-helm-chrt"
CHART_VERSION = "1.2.4"
RELEASE = "zabbix"
NAMESPACE = "monitoring"
PROXY_HOSTNAME = "litellm-lab-k8s-proxy"
K8S_HOST = "litellm-lab-k8s"
ENTRY_HOST = "litellm-lab-entry"
GROUPS = {"vm": "litellm-lab/vm", "k8s": "litellm-lab/k8s", "service": "litellm-lab/service"}
TEMPLATE_LINUX_ACTIVE = "Linux by Zabbix agent active"
TEMPLATE_K8S_STATE = "Kubernetes cluster state by HTTP"
ACTION_NODES = "litellm-lab: register vm nodes"
ACTION_BASTION = "litellm-lab: register bastion"
SA_SECRET = "zabbix-service-account"      # チャートが作る ServiceAccount のトークンの Secret
KSM_ENDPOINT = f"{RELEASE}-kube-state-metrics"
DEFAULT_ADMIN = ("Admin", "zabbix")
TOKEN_NAME = "litellm-lab"
ENTRY_CHECKS = [("liveliness", "/health/liveliness"), ("readiness", "/health/readiness")]


class ZabbixApi:
    """Zabbix の JSON-RPC（7.0。認証は Authorization: Bearer <トークン>）。"""

    def __init__(self, url: str, token: str | None = None):
        self.url, self.token = url, token

    def call(self, method: str, params=None, auth: bool = True):
        body = {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}, "id": 1}
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            raise LabError(f"Zabbix の API が HTTP {e.code} を返しました（{method}）") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LabError(f"Zabbix の API に届きません（{self.url}）: {e}") from e
        if "error" in out:
            err = out["error"]
            raise LabError(f"Zabbix の API が失敗しました（{method}）: {err.get('message')} {err.get('data', '')}")
        return out["result"]

    def version(self) -> str:
        return self.call("apiinfo.version", auth=False)

    def get_one(self, obj: str, **filt) -> dict | None:
        found = self.call(f"{obj}.get", {"filter": filt, "output": "extend"})
        return found[0] if found else None


# ---- 接続とトークン ----
def _try_version(url: str) -> bool:
    try:
        ZabbixApi(url).version()
        return True
    except LabError:
        return False


def api(env: Env, infra=None) -> ZabbixApi:
    """API のクライアント。LAB_ZABBIX_VM=true なら転送口（踏み台経由）に向け、トークンが無ければ初回の設定（Admin のパスワードの
    変更と API トークンの発行）をして .env に書く。provided は .env の URL とトークン。"""
    url = env.zabbix_api_url
    if not url:
        raise LabError("Zabbix の API の URL がありません（LAB_ZABBIX_API_URL。LAB_ZABBIX_VM=true なら転送口が自動）")
    if env.zabbix_vm:
        if infra is None:
            infra = vm.load_infra(env)
        vm.ensure_zabbix_forward(env, infra)
        wait_until(600, "Zabbix の web と API の起動", lambda: _try_version(url))
    token = env.get("LAB_ZABBIX_API_TOKEN")
    if token:
        client = ZabbixApi(url, token)
        try:
            client.call("host.get", {"output": ["hostid"], "limit": 1})
            return client
        except LabError as e:
            if not env.zabbix_vm:
                raise
            warn(f"保存してある Zabbix の API トークンが使えません（{e}）。作り直します")
    if not env.zabbix_vm:
        raise LabError("Zabbix の API トークン（LAB_ZABBIX_API_TOKEN）がありません（provided は用意する側から受け取る）")
    return _bootstrap_token(env, url)


def _bootstrap_token(env: Env, url: str) -> ZabbixApi:
    """初回: Admin の初期パスワードでログインしてパスワードを変え、API トークンを作る（サーバーの DB の初期化を待つ）。"""
    client = ZabbixApi(url)
    password = env.get("LAB_ZABBIX_ADMIN_PASSWORD") or gen_secret(18)

    def login() -> bool:
        for candidate in (password, DEFAULT_ADMIN[1]):
            try:
                client.token = client.call("user.login", {"username": DEFAULT_ADMIN[0], "password": candidate}, auth=False)
                client._logged_in_with = candidate   # noqa: SLF001
                return True
            except LabError as e:
                if "Login name or password is incorrect" in str(e) or "incorrect" in str(e):
                    continue
                return False   # DB の初期化の途中など。待つ
        return False

    wait_until(900, "Zabbix サーバーの初期化（Admin でのサインイン）", login)
    if client._logged_in_with == DEFAULT_ADMIN[1]:   # noqa: SLF001
        admin = client.get_one("user", username=DEFAULT_ADMIN[0])
        client.call("user.update", {"userid": admin["userid"], "passwd": password, "current_passwd": DEFAULT_ADMIN[1]})
        info("Zabbix の Admin の初期パスワードを変えました（.env の LAB_ZABBIX_ADMIN_PASSWORD）")
    env.set("LAB_ZABBIX_ADMIN_PASSWORD", password)
    admin = client.get_one("user", username=DEFAULT_ADMIN[0])
    for old in client.call("token.get", {"filter": {"name": TOKEN_NAME}, "output": ["tokenid"]}):
        client.call("token.delete", [old["tokenid"]])
    tokenid = client.call("token.create", {"name": TOKEN_NAME, "userid": admin["userid"]})["tokenids"][0]
    token = client.call("token.generate", [tokenid])[0]["token"]
    client.call("user.logout", [], auth=True)
    env.set("LAB_ZABBIX_API_TOKEN", token)
    info("Zabbix の API トークンを作りました（.env の LAB_ZABBIX_API_TOKEN）")
    return ZabbixApi(url, token)


# ---- 設定（冪等） ----
def ensure_groups(client: ZabbixApi) -> dict[str, str]:
    out = {}
    for key, name in GROUPS.items():
        g = client.get_one("hostgroup", name=name)
        if g is None:
            g = {"groupid": client.call("hostgroup.create", {"name": name})["groupids"][0]}
            info(f"Zabbix のホストグループ {name} を作りました")
        out[key] = g["groupid"]
    return out


def template_id(client: ZabbixApi, name: str) -> str:
    t = client.get_one("template", host=name)
    if t is None:
        raise LabError(f"Zabbix にテンプレート {name!r} がありません（7.0 の同梱のテンプレートが要ります）")
    return t["templateid"]


def ensure_autoregistration(client: ZabbixApi, name: str, metadata: str, groupid: str, templateid: str) -> None:
    """agent の自動登録のアクション: HostMetadata に metadata を含むホストをグループに入れ、テンプレートを紐付ける。"""
    want = {"name": name, "eventsource": 2, "status": 0,
            "filter": {"evaltype": 0, "conditions": [{"conditiontype": 24, "operator": 2, "value": metadata}]},
            "operations": [{"operationtype": 4, "opgroup": [{"groupid": groupid}]},
                           {"operationtype": 6, "optemplate": [{"templateid": templateid}]}]}
    found = client.call("action.get", {"filter": {"name": name}, "output": "extend", "selectOperations": "extend",
                                       "selectFilter": "extend"})
    if not found:
        client.call("action.create", want)
        info(f"Zabbix の自動登録のアクション {name!r} を作りました")
        return
    cur = found[0]
    cur_groups = {g["groupid"] for op in cur["operations"] for g in op.get("opgroup", [])}
    cur_templates = {t["templateid"] for op in cur["operations"] for t in op.get("optemplate", [])}
    cur_values = {c["value"] for c in cur["filter"].get("conditions", [])}
    if cur_groups != {groupid} or cur_templates != {templateid} or cur_values != {metadata} or cur.get("status") != "0":
        client.call("action.update", {"actionid": cur["actionid"], **want})
        info(f"Zabbix の自動登録のアクション {name!r} を直しました")


def ensure_proxy(client: ZabbixApi, name: str) -> str:
    p = client.get_one("proxy", name=name)
    if p is None:
        pid = client.call("proxy.create", {"name": name, "operating_mode": 0})["proxyids"][0]
        info(f"Zabbix のプロキシ {name} を登録しました")
        return pid
    return p["proxyid"]


def ensure_host(client: ZabbixApi, host: str, groupids: list[str], templateids: list[str], macros: dict[str, str],
                proxyid: str | None = None, tags: dict[str, str] | None = None) -> str:
    """ホスト（エージェント無し。HTTP のアイテムのためのダミーのインターフェース付き）。無ければ作り、違えば直す。"""
    want = {"host": host, "name": host, "status": 0,
            "groups": [{"groupid": g} for g in groupids],
            "templates": [{"templateid": t} for t in templateids],
            "macros": [{"macro": k, "value": v} for k, v in sorted(macros.items())],
            "tags": [{"tag": k, "value": v} for k, v in sorted((tags or {}).items())]}
    if proxyid:
        want.update({"monitored_by": 1, "proxyid": proxyid})
    found = client.call("host.get", {"filter": {"host": host}, "output": "extend", "selectParentTemplates": ["templateid"],
                                     "selectMacros": ["macro", "value"], "selectHostGroups": ["groupid"], "selectTags": "extend"})
    if not found:
        want["interfaces"] = [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1", "dns": "", "port": "10050"}]
        hostid = client.call("host.create", want)["hostids"][0]
        info(f"Zabbix のホスト {host} を作りました")
        return hostid
    cur = found[0]
    same = (
        {t["templateid"] for t in cur["parentTemplates"]} == set(templateids)
        and {g["groupid"] for g in cur["hostgroups"]} == set(groupids)
        and {m["macro"]: m["value"] for m in cur["macros"] if m["macro"] in macros} == macros
        and {t["tag"]: t["value"] for t in cur.get("tags", [])} == (tags or {})
        and (not proxyid or (cur.get("monitored_by") == "1" and cur.get("proxyid") == proxyid))
        and cur.get("status") == "0"
    )
    if not same:
        # macros の更新は全部の置き換えなので、テンプレートが持つものを消さないように、こちらが管理する鍵だけ差し替える
        others = [{"macro": m["macro"], "value": m["value"]} for m in cur["macros"] if m["macro"] not in macros]
        want["macros"] = others + want["macros"]
        client.call("host.update", {"hostid": cur["hostid"], **want})
        info(f"Zabbix のホスト {host} を直しました")
    return cur["hostid"]


def ensure_http_check(client: ZabbixApi, hostid: str, host: str, name: str, url: str) -> None:
    """HTTP agent のアイテム（1 分ごと、200 以外は値なし）と、3 分値が無ければ発火するトリガー。"""
    key = f"lab.http[{name}]"
    item = client.call("item.get", {"hostids": hostid, "filter": {"key_": key}, "output": "extend"})
    want = {"name": f"入口 {name}", "key_": key, "type": 19, "value_type": 4, "delay": "60s", "url": url,
            "status_codes": "200", "timeout": "10s", "retrieve_mode": 0, "output_format": 0}
    if not item:
        client.call("item.create", {"hostid": hostid, **want})
        info(f"Zabbix のアイテム {key}（{url}）を作りました")
    elif item[0]["url"] != url or item[0]["status"] != "0":
        client.call("item.update", {"itemid": item[0]["itemid"], "url": url, "status": 0})
    desc = f"litellm-lab: {name} が 3 分応答していない"
    trig = client.call("trigger.get", {"hostids": hostid, "filter": {"description": desc}, "output": "extend"})
    expr = f"nodata(/{host}/{key},180s)=1"
    if not trig:
        client.call("trigger.create", {"description": desc, "expression": expr, "priority": 4})
        info(f"Zabbix のトリガー {desc!r} を作りました")
    elif trig[0]["expression"] != expr:
        client.call("trigger.update", {"triggerid": trig[0]["triggerid"], "expression": expr})


# ---- Kubernetes（Helm チャート） ----
def chart_values(env: Env, server: str) -> dict:
    """zabbix-proxy（能動。サーバーは踏み台）と kube-state-metrics。チャートの agent は入れない（VM の agent 2 と重なる）。"""
    return {
        "kubeStateMetrics": {"enabled": True},
        "zabbixAgent": {"enabled": False},
        "zabbixProxy": {
            "enabled": True,
            "env": [{"name": "ZBX_PROXYMODE", "value": 0}, {"name": "ZBX_HOSTNAME", "value": PROXY_HOSTNAME},
                    {"name": "ZBX_SERVER_HOST", "value": server}, {"name": "ZBX_DEBUGLEVEL", "value": 3},
                    {"name": "ZBX_CACHESIZE", "value": "64M"}, {"name": "ZBX_STARTJAVAPOLLERS", "value": 0},
                    {"name": "ZBX_PROXYCONFIGFREQUENCY", "value": 10}],
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
        },
    }


def ensure_chart(env: Env, server: str) -> None:
    from .core import BUILD_DIR, json_output, short_hash
    values = chart_values(env, server)
    values_file = BUILD_DIR / "zabbix-values.json"
    values_file.write_text(json.dumps(values, indent=2) + "\n")
    values_hash = short_hash(values_file.read_text() + CHART_VERSION)
    current = json_output(helm("-n", NAMESPACE, "get", "values", RELEASE, "-o", "json"), default={}) or {}
    if current.get("labValuesHash") == values_hash:
        ok(f"Zabbix の Helm のリリースは最新です（値のハッシュ {values_hash}）")
        return
    repos = output(helm("repo", "list", "-o", "json"), check=False)
    if CHART_REPO_NAME not in repos:
        run(helm("repo", "add", CHART_REPO_NAME, CHART_REPO), quiet=True)
    run(helm("repo", "update", CHART_REPO_NAME), quiet=True)
    run(helm("upgrade", "--install", RELEASE, f"{CHART_REPO_NAME}/{CHART}", "--version", CHART_VERSION,
             "-n", NAMESPACE, "--create-namespace", "-f", str(values_file), "--set-string", f"labValuesHash={values_hash}",
             "--wait", "--timeout", "10m"))
    ok("Zabbix の Helm チャート（zabbix-proxy、kube-state-metrics）を入れました")


def service_account_token() -> str:
    data = output(kc("-n", NAMESPACE, "get", "secret", SA_SECRET, "-o", "jsonpath={.data.token}"), check=False).strip()
    if not data:
        raise LabError(f"Zabbix の ServiceAccount のトークン（Secret {SA_SECRET}）が読めません")
    import base64
    return base64.b64decode(data).decode()


# ---- 段階 ----
def setup(env: Env) -> None:
    """./lab zabbix: サーバー（managed は踏み台の VM）→ API トークン → グループ → 自動登録 → Helm チャート → プロキシ →
    Kubernetes のホスト → 入口の HTTP 監視。"""
    if not env.zabbix_enabled:
        ok("Zabbix は使いません（LAB_ZABBIX_VM=false で LAB_ZABBIX_SERVER が空）")
        return
    infra = vm.load_infra(env)
    server = env.zabbix_server
    if not server:
        raise LabError("LAB_ZABBIX_SERVER（agent と proxy が送る宛先 host:10051）がありません")
    client = api(env, infra)
    info(f"Zabbix {client.version()}（{env.zabbix_api_url}）")
    groups = ensure_groups(client)
    linux = template_id(client, TEMPLATE_LINUX_ACTIVE)
    ensure_autoregistration(client, ACTION_NODES, vm.ZABBIX_NODE_METADATA, groups["vm"], linux)
    ensure_autoregistration(client, ACTION_BASTION, "litellm-lab-bastion", groups["vm"], linux)
    ensure_chart(env, server)
    proxyid = ensure_proxy(client, PROXY_HOSTNAME)
    k8s_macros = {"{$KUBE.API.URL}": "https://kubernetes.default.svc.cluster.local:443",
                  "{$KUBE.API.TOKEN}": service_account_token(),
                  "{$KUBE.STATE.ENDPOINT.NAME}": KSM_ENDPOINT}
    ensure_host(client, K8S_HOST, [groups["k8s"]], [template_id(client, TEMPLATE_K8S_STATE)], k8s_macros, proxyid,
                tags={"lab": "k8s"})
    entry = ensure_host(client, ENTRY_HOST, [groups["service"]], [], {}, proxyid, tags={"lab": "entry"})
    for name, path in ENTRY_CHECKS:
        ensure_http_check(client, entry, ENTRY_HOST, name, f"https://{env.hostname}{path}")
    ok(f"Zabbix の設定がそろいました（web: http://127.0.0.1:{env.zabbix_local_port}/ Admin。ホスト: VM {len(infra.nodes)} 台 ＋ 踏み台は自動登録、"
       f"{K8S_HOST}、{ENTRY_HOST}）" if env.zabbix_vm else f"Zabbix の設定がそろいました（{env.zabbix_api_url}）")


def summary(env: Env) -> list[str]:
    """./lab status 向け: ホストと最新値の有無。"""
    if not env.zabbix_enabled:
        return ["Zabbix:          使わない"]
    try:
        client = api(env)
        hosts = client.call("host.get", {"output": ["host", "status"], "selectInterfaces": ["available"],
                                         "groupids": list(ensure_groups(client).values())})
        lines = []
        for h in sorted(hosts, key=lambda x: x["host"]):
            items = client.call("item.get", {"hostids": h["hostid"], "output": ["lastclock"], "filter": {"status": 0},
                                             "sortfield": "lastclock", "sortorder": "DESC", "limit": 1})
            last = int(items[0]["lastclock"]) if items and items[0].get("lastclock") else 0
            lines.append(f"  {h['host']}: " + ("最新値あり" if last else "値なし"))
        return [f"Zabbix:          {len(hosts)} ホスト（{env.zabbix_api_url}）", *lines]
    except LabError as e:
        return [f"Zabbix:          届きません（{e}）"]


def ready_hosts(env: Env, expected: int) -> bool:
    """試験向け: 監視対象のホストが expected 台以上あり、どれも 1 つは最新値を持つか。"""
    client = api(env)
    groups = ensure_groups(client)
    hosts = client.call("host.get", {"output": ["hostid", "host"], "groupids": list(groups.values())})
    if len(hosts) < expected:
        return False
    for h in hosts:
        items = client.call("item.get", {"hostids": h["hostid"], "output": ["lastclock"], "filter": {"status": 0},
                                         "sortfield": "lastclock", "sortorder": "DESC", "limit": 1})
        if not items or not int(items[0].get("lastclock") or 0):
            return False
    return True


__all__ = ["ZabbixApi", "api", "setup", "summary", "ready_hosts", "chart_values", "NS", "render", "succeeds"]
