"""GCP のクラウド固有層（LAB_TARGET=gcp-vm。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章、
.history/2026-10-31-内部LBとVM側の防御とZabbix/ 3.2 節）。

provision（managed）: VPC とファイアウォール（通す規則だけ。守りは VM の nftables）、内部パススルー ネットワーク ロードバランサ
（静的内部 IP、TCP のヘルスチェック、バックエンドサービス、ゾーンごとの非マネージドインスタンスグループ、転送ルール）、
Cloud Storage のバケット 1 つ（オブジェクト保持ロック）、サービスアカウントと HMAC キー、カスタムロール、Compute Engine 3 台
（C4A、Ubuntu 24.04 arm64。サービスアカウント無し。startup-script は使わず SSH で初期化する）、中継（出口プロキシ。踏み台と
Zabbix サーバーも兼ねる）の VM。資源は名前（<env.vm_prefix>-…）とラベル lab-prefix で探し、無ければ作る。
setup（両モード）: .env の識別子（<ゾーン>/<名前>、転送ルールの名前、バケット名）を読む。
資格情報はアプリケーション既定の資格情報（gcloud auth application-default login）。Google のライブラリは関数の中で読み込む。
"""

from __future__ import annotations

import contextlib
import json
import time

from .. import objectstore as objstore
from .. import render
from ..core import Env, LabError, info, ok, warn
from . import (
    GCP_DNS_IPS, GCP_HEALTH_CHECK_RANGES, GCP_META_IPS, GCS_ROLE_LISTER, GCS_ROLE_WRITER, NODE_COUNT, OPERATOR_USER, PROXY_PORT,
    STATIC_KEY_USERS, ZABBIX_SERVER_PORT, Infra, Node, gcs_bindings, network_plan,
)

LABEL_KEY = "lab-prefix"
IMAGE_PROJECT = "ubuntu-os-cloud"
IMAGE_FAMILY = "ubuntu-2404-lts-arm64"
PROXY_IMAGE_FAMILY = "ubuntu-2404-lts-amd64"
PROXY_MACHINE_TYPE = "e2-small"            # 中継だけ
PROXY_MACHINE_TYPE_ZABBIX = "e2-medium"    # 中継 ＋ Zabbix サーバー（4 GiB）
DISK_TYPE = "hyperdisk-balanced"   # C4A は pd-* を使えない
S3_ENDPOINT = "https://storage.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# 内部ロードバランサのヘルスチェック（TCP 80。5 秒 × 2 回で外す）
HC_INTERVAL = 5
HC_THRESHOLD = 2
# カスタムロール（custom:… → ロール ID の末尾と権限）
CUSTOM_ROLES = {
    GCS_ROLE_WRITER: ("writer", ["storage.objects.create", "storage.objects.setRetention"]),
    GCS_ROLE_LISTER: ("lister", ["storage.objects.list", "storage.buckets.get"]),
}


def _compute():
    from google.cloud import compute_v1
    return compute_v1


def _names(env: Env) -> dict[str, str]:
    p = env.vm_prefix
    return {"network": f"{p}-net", "subnet": f"{p}-subnet", "fw_web": f"{p}-web", "fw_internal": f"{p}-internal",
            "fw_ssh": f"{p}-ssh", "fw_proxy": f"{p}-proxy-in", "fw_egress_deny": f"{p}-egress-deny",
            "fw_egress_internal": f"{p}-egress-internal", "tag": f"{p}-node", "proxy_tag": f"{p}-proxy",
            "address": f"{p}-entry", "node_sa": f"{p}-node", "node_role": p.replace("-", "_") + "_node",
            "lb_address": f"{p}-lb", "lb_health_check": f"{p}-lb-hc", "lb_backend": f"{p}-lb-backend",
            "lb_forwarding_rule": f"{p}-lb", "lb_group": f"{p}-lb-group",   # インスタンスグループは <lb_group>-<ゾーン>
            "proxy": f"{p}-proxy", "bucket": f"{env.gcp_prefix}-{env.gcp_project}"}


def _sa_email(env: Env, account_id: str) -> str:
    return f"{account_id}@{env.gcp_project}.iam.gserviceaccount.com"


def _custom_role_name(env: Env, role: str) -> str:
    if not role.startswith("custom:"):
        return role
    suffix = CUSTOM_ROLES[role][0]
    return f"projects/{env.gcp_project}/roles/{env.vm_prefix.replace('-', '_')}_{suffix}"


_zones_warned: list[str] = []


def assign_zones(available: list[str], count: int = NODE_COUNT) -> list[str]:
    """台 1〜count をゾーンに割り振る。使えるゾーンが台数より少なければ順に繰り返す（東京の C4A は b と c の 2 つ。配備で判明）。"""
    if not available:
        raise LabError("使えるゾーンがありません")
    return [available[i % len(available)] for i in range(count)]


def _zones(env: Env) -> list[str]:
    """リージョンの UP なゾーンのうち、LAB_GCP_MACHINE_TYPE が置けるゾーンを名前順に拾い、台に割り振る。"""
    compute = _compute()
    from google.api_core import exceptions as gexc
    zones = sorted(z.name for z in compute.ZonesClient().list(project=env.gcp_project)
                   if z.region.rsplit("/", 1)[-1] == env.gcp_region and z.status == "UP")
    mt = compute.MachineTypesClient()
    usable = []
    for zone in zones:
        with contextlib.suppress(gexc.NotFound):
            mt.get(project=env.gcp_project, zone=zone, machine_type=env.gcp_machine_type)
            usable.append(zone)
    if not usable:
        raise LabError(f"{env.gcp_region} に {env.gcp_machine_type} を置けるゾーンがありません（ゾーン: {', '.join(zones) or '無し'}）")
    if len(usable) < NODE_COUNT and not _zones_warned:
        _zones_warned.append(env.gcp_region)
        warn(f"{env.gcp_machine_type} が使えるゾーンは {', '.join(usable)} の {len(usable)} つなので、{NODE_COUNT} 台を繰り返して割り振ります")
    return assign_zones(usable)


def _NotFound():
    """存在確認で捕まえる例外（google.api_core の NotFound）。他の例外（呼び方の誤り、権限）は隠さず止める。"""
    from google.api_core import exceptions as gexc
    return gexc.NotFound


# ---- 操作者、名前 ----
def identity(env: Env) -> dict:
    import google.auth
    from google.auth import exceptions as gauth_exc
    try:
        creds, project = google.auth.default(scopes=[SCOPE])
    except gauth_exc.DefaultCredentialsError as e:
        raise LabError("GCP の資格情報（ADC）がありません。gcloud auth application-default login を実行してください"
                       "（gcloud が Python の版で起動しないなら CLOUDSDK_PYTHON=/opt/homebrew/bin/python3 を前に付ける）") from e
    principal = getattr(creds, "service_account_email", None) or getattr(creds, "_account", None) or ""
    if not principal:
        # 利用者の ADC は主体のメールを持たないので、tokeninfo で引く（バケットの IAM に操作者を足すため。2026-10-31。
        # userinfo は serviceusage の権限を求めて 403 になることがある）
        try:
            import urllib.parse
            import urllib.request
            from google.auth.transport.requests import Request
            if not creds.valid:
                creds.refresh(Request())
            url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"access_token": creds.token})
            with urllib.request.urlopen(url, timeout=15) as r:
                principal = json.load(r).get("email", "")
        except Exception:  # noqa: BLE001 分からなければ空のまま
            principal = ""
    return {"account": env.gcp_project, "principal": principal or "(ADC)", "region": env.gcp_region,
            "adc_project": project or ""}


REQUIRED_APIS = ["compute.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com",
                 "storage.googleapis.com"]


def ensure_apis(env: Env) -> None:
    """要る API（Compute Engine、IAM、Resource Manager、Cloud Storage）をプロジェクトで有効にする（Service Usage の REST。
    有効化は無料。gcloud を使わない）。"""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=[SCOPE])
    session = AuthorizedSession(creds)
    base = f"https://serviceusage.googleapis.com/v1/projects/{env.gcp_project}/services"
    r = session.get(base, params={"filter": "state:ENABLED", "pageSize": 200}, timeout=60)
    r.raise_for_status()
    enabled = {svc["config"]["name"] for svc in r.json().get("services", [])}
    missing = [a for a in REQUIRED_APIS if a not in enabled]
    if not missing:
        ok("GCP の API は有効です")
        return
    r = session.post(f"{base}:batchEnable", json={"serviceIds": missing}, timeout=60)
    r.raise_for_status()
    op = r.json()
    for _ in range(60):
        if op.get("done"):
            break
        time.sleep(5)
        op = session.get(f"https://serviceusage.googleapis.com/v1/{op['name']}", timeout=60).json()
    if op.get("error"):
        raise LabError(f"GCP の API の有効化に失敗しました: {op['error']}")
    info(f"GCP の API を有効にしました: {', '.join(missing)}")


def s3_endpoint(env: Env) -> str:
    return S3_ENDPOINT


# ---- ネットワーク ----
def ensure_network(env: Env) -> dict:
    """VPC（カスタムモード）、リージョンのサブネット 1 つ（Private Google Access あり）、ファイアウォール。
    ファイアウォールは「通す」規則だけ（カスタム VPC は既定で入りを全部拒むため）。守りは VM の nftables
    （変更設計書 2026-10-31 3.2 節）。80/443 はサブネットとロードバランサのヘルスチェックの送信元から。"""
    compute = _compute()
    names = _names(env)
    project = env.gcp_project
    plan = network_plan(env.vm_cidr, subnets_per_node=False)
    nets = compute.NetworksClient()
    try:
        network = nets.get(project=project, network=names["network"])
    except _NotFound():
        nets.insert(project=project, network_resource=compute.Network(
            name=names["network"], auto_create_subnetworks=False)).result()
        network = nets.get(project=project, network=names["network"])
        info(f"VPC ネットワーク {names['network']} を作りました")
    subnets = compute.SubnetworksClient()
    try:
        subnet = subnets.get(project=project, region=env.gcp_region, subnetwork=names["subnet"])
    except _NotFound():
        subnets.insert(project=project, region=env.gcp_region, subnetwork_resource=compute.Subnetwork(
            name=names["subnet"], network=network.self_link, ip_cidr_range=plan["subnets"][0],
            private_ip_google_access=True)).result()
        subnet = subnets.get(project=project, region=env.gcp_region, subnetwork=names["subnet"])
        info(f"サブネット {names['subnet']}（{plan['subnets'][0]}、{env.gcp_region}）を作りました")
    fws = compute.FirewallsClient()
    existing = {f.name for f in fws.list(project=project) if f.name in names.values()}
    cidr = plan["subnets"][0]
    wanted = {
        names["fw_web"]: compute.Firewall(
            name=names["fw_web"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=["80", "443"])],
            source_ranges=[cidr, *GCP_HEALTH_CHECK_RANGES], target_tags=[names["tag"]]),
        names["fw_internal"]: compute.Firewall(
            name=names["fw_internal"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="all")], source_ranges=[cidr],
            target_tags=[names["tag"], names["proxy_tag"]]),
        # 22 の送信元は allow_ssh_from が置き換える。作るときは誰にも開けない
        names["fw_ssh"]: compute.Firewall(
            name=names["fw_ssh"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=["22"])], source_ranges=["127.0.0.1/32"],
            target_tags=[names["tag"], names["proxy_tag"]]),
    }
    if env.egress_proxy_vm:
        wanted[names["fw_proxy"]] = compute.Firewall(
            name=names["fw_proxy"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=[str(PROXY_PORT), str(ZABBIX_SERVER_PORT)])],
            source_ranges=[cidr], target_tags=[names["proxy_tag"]])
    for name, fw in wanted.items():
        if name not in existing:
            fws.insert(project=project, firewall_resource=fw).result()
            info(f"ファイアウォールの規則 {name} を作りました")
        elif name == names["fw_web"]:
            cur = fws.get(project=project, firewall=name)
            if sorted(cur.source_ranges) != sorted(fw.source_ranges):
                fws.patch(project=project, firewall=name, firewall_resource=compute.Firewall(source_ranges=list(fw.source_ranges))).result()
                info(f"ファイアウォールの規則 {name} の送信元を {', '.join(fw.source_ranges)} にしました")
    # 外向きの遮断はクラウドでなく VM の nftables で行う（2026-10-31）。前の版の規則が残っていれば消す
    stale = [names["fw_egress_deny"], names["fw_egress_internal"]] + ([] if env.egress_proxy_vm else [names["fw_proxy"]])
    for name in stale:
        if name in existing:
            fws.delete(project=project, firewall=name).result()
            info(f"ファイアウォールの規則 {name} を消しました")
    ok("ネットワークは最新です")
    return {"network": network.self_link, "subnet": subnet.self_link, "plan": plan, "zones": _zones(env),
            "tag": names["tag"], "proxy_tag": names["proxy_tag"]}


def _allow_from(env: Env, rule: str, cidrs: list[str], what: str) -> None:
    compute = _compute()
    fws = compute.FirewallsClient()
    name = _names(env)[rule]
    try:
        fw = fws.get(project=env.gcp_project, firewall=name)
    except _NotFound():
        return
    if sorted(fw.source_ranges) == sorted(cidrs):
        return
    fws.patch(project=env.gcp_project, firewall=name, firewall_resource=compute.Firewall(source_ranges=cidrs)).result()
    info(f"{what} の許可を {', '.join(cidrs)} だけにしました（前: {', '.join(fw.source_ranges) or '無し'}）")


def allow_ssh_from(env: Env, cidr: str) -> None:
    _allow_from(env, "fw_ssh", [cidr], "22")


def allow_proxy_from(env: Env, cidr: str) -> None:
    """中継（3128）と Zabbix サーバー（10051）の送信元: サブネット ＋ 操作者の IP（Mac も中継を通すため）。"""
    plan = network_plan(env.vm_cidr, subnets_per_node=False)
    _allow_from(env, "fw_proxy", [plan["subnets"][0], cidr], f"中継（{PROXY_PORT}）")


def health_check_ranges(env: Env) -> list[str]:
    """内部ロードバランサのヘルスチェックの送信元（VM の nftables で通す）。"""
    return list(GCP_HEALTH_CHECK_RANGES)


def meta_ips(env: Env) -> list[str]:
    """VM が中継を通さず直接届く必要があるクラウドの内部アドレス（メタデータサーバーが DNS と NTP も兼ねる）。"""
    return list(GCP_META_IPS)


def dns_ips(env: Env) -> list[str]:
    """クラウドの再帰リゾルバ（cert-manager の DNS-01 の伝搬確認）。"""
    return list(GCP_DNS_IPS)


# ---- 内部ロードバランサ（内部パススルー ネットワーク ロードバランサ。変更設計書 2026-10-31 3.2 節） ----
def _region_path(env: Env, kind: str, name: str) -> str:
    return f"projects/{env.gcp_project}/regions/{env.gcp_region}/{kind}/{name}"


def ensure_load_balancer(env: Env, net: dict, nodes: list[Node]) -> tuple[str, str]:
    """静的内部 IP → TCP 80 のヘルスチェック → ゾーンごとの非マネージドインスタンスグループ（3 台を入れる）→ バックエンドサービス →
    転送ルール（80 と 443、内部 IP）。あれば使い、無ければ作る。(内部 IP, 転送ルールの名前) を返す。"""
    compute = _compute()
    names, project, region = _names(env), env.gcp_project, env.gcp_region
    plan = net["plan"]
    addrs = compute.AddressesClient()
    try:
        addr = addrs.get(project=project, region=region, address=names["lb_address"])
    except _NotFound():
        addrs.insert(project=project, region=region, address_resource=compute.Address(
            name=names["lb_address"], address_type="INTERNAL", purpose="GCE_ENDPOINT", subnetwork=net["subnet"],
            address=plan["lb_ips"][0], labels={LABEL_KEY: env.vm_prefix})).result()
        addr = addrs.get(project=project, region=region, address=names["lb_address"])
        info(f"ロードバランサの内部 IP {addr.address} を確保しました")
    hcs = compute.RegionHealthChecksClient()
    try:
        hc = hcs.get(project=project, region=region, health_check=names["lb_health_check"])
    except _NotFound():
        hcs.insert(project=project, region=region, health_check_resource=compute.HealthCheck(
            name=names["lb_health_check"], type_="TCP", tcp_health_check=compute.TCPHealthCheck(port=80),
            check_interval_sec=HC_INTERVAL, timeout_sec=HC_INTERVAL, unhealthy_threshold=HC_THRESHOLD,
            healthy_threshold=HC_THRESHOLD)).result()
        hc = hcs.get(project=project, region=region, health_check=names["lb_health_check"])
        info(f"ヘルスチェック {hc.name}（TCP 80、{HC_INTERVAL} 秒 × {HC_THRESHOLD} 回）を作りました")
    groups = _ensure_instance_groups(env, net, nodes)
    bes = compute.RegionBackendServicesClient()
    backends = [compute.Backend(group=g, balancing_mode="CONNECTION") for g in groups]
    try:
        be = bes.get(project=project, region=region, backend_service=names["lb_backend"])
        have = sorted(b.group.split("/projects/")[-1] for b in be.backends)
        want = sorted(g.split("/projects/")[-1] for g in groups)
        if have != want:
            bes.patch(project=project, region=region, backend_service=names["lb_backend"],
                      backend_service_resource=compute.BackendService(backends=backends, fingerprint=be.fingerprint)).result()
            info("バックエンドサービスのインスタンスグループをそろえました")
    except _NotFound():
        bes.insert(project=project, region=region, backend_service_resource=compute.BackendService(
            name=names["lb_backend"], load_balancing_scheme="INTERNAL", protocol="TCP", network=net["network"],
            backends=backends, health_checks=[hc.self_link],
            connection_draining=compute.ConnectionDraining(draining_timeout_sec=10))).result()
        info(f"バックエンドサービス {names['lb_backend']}（TCP、3 台）を作りました")
    frs = compute.ForwardingRulesClient()
    try:
        fr = frs.get(project=project, region=region, forwarding_rule=names["lb_forwarding_rule"])
    except _NotFound():
        frs.insert(project=project, region=region, forwarding_rule_resource=compute.ForwardingRule(
            name=names["lb_forwarding_rule"], load_balancing_scheme="INTERNAL", I_p_protocol="TCP", ports=["80", "443"],
            network=net["network"], subnetwork=net["subnet"], I_p_address=addr.self_link,
            backend_service=_region_path(env, "backendServices", names["lb_backend"]),
            labels={LABEL_KEY: env.vm_prefix})).result()
        fr = frs.get(project=project, region=region, forwarding_rule=names["lb_forwarding_rule"])
        info(f"転送ルール {fr.name}（{fr.I_p_address}、80/443）を作りました")
    return fr.I_p_address, fr.name


def _ensure_instance_groups(env: Env, net: dict, nodes: list[Node]) -> list[str]:
    """ゾーンごとの非マネージドインスタンスグループに、そのゾーンの台を入れる。グループの selfLink の一覧を返す。"""
    compute = _compute()
    names, project = _names(env), env.gcp_project
    igs = compute.InstanceGroupsClient()
    links = []
    by_zone: dict[str, list[Node]] = {}
    for n in nodes:
        by_zone.setdefault(n.zone, []).append(n)
    for zone, members in sorted(by_zone.items()):
        name = f"{names['lb_group']}-{zone}"
        try:
            ig = igs.get(project=project, zone=zone, instance_group=name)
        except _NotFound():
            igs.insert(project=project, zone=zone, instance_group_resource=compute.InstanceGroup(
                name=name, network=net["network"], subnetwork=net["subnet"])).result()
            ig = igs.get(project=project, zone=zone, instance_group=name)
            info(f"インスタンスグループ {name} を作りました")
        current = {i.instance.rsplit("/", 1)[-1] for i in igs.list_instances(
            project=project, zone=zone, instance_group=name,
            instance_groups_list_instances_request_resource=compute.InstanceGroupsListInstancesRequest())}
        missing = [m for m in members if m.id.split("/", 1)[1] not in current]
        if missing:
            igs.add_instances(project=project, zone=zone, instance_group=name,
                              instance_groups_add_instances_request_resource=compute.InstanceGroupsAddInstancesRequest(
                                  instances=[compute.InstanceReference(
                                      instance=f"projects/{project}/zones/{zone}/instances/{m.id.split('/', 1)[1]}") for m in missing])).result()
            info(f"インスタンスグループ {name} に {', '.join(m.id for m in missing)} を入れました")
        links.append(ig.self_link)
    return links


def describe_entry_ip(env: Env, entry_ip_id: str) -> str:
    """入口の識別子（転送ルールの名前）→ ロードバランサの内部 IP。"""
    try:
        return _compute().ForwardingRulesClient().get(project=env.gcp_project, region=env.gcp_region,
                                                     forwarding_rule=entry_ip_id).I_p_address
    except _NotFound():
        raise LabError(f"転送ルール {entry_ip_id}（LAB_VM_ENTRY_IP_ID）が {env.gcp_region} にありません")


def lb_targets_healthy(env: Env, infra: Infra) -> dict[str, bool]:
    """ロードバランサが見る 3 台の健全性（Node.id → healthy か）。"""
    compute = _compute()
    frs = compute.ForwardingRulesClient()
    fr = frs.get(project=env.gcp_project, region=env.gcp_region, forwarding_rule=infra.entry_ip_id)
    bes = compute.RegionBackendServicesClient()
    be_name = fr.backend_service.rsplit("/", 1)[-1]
    be = bes.get(project=env.gcp_project, region=env.gcp_region, backend_service=be_name)
    out: dict[str, bool] = {}
    for b in be.backends:
        health = bes.get_health(project=env.gcp_project, region=env.gcp_region, backend_service=be_name,
                                resource_group_reference_resource=compute.ResourceGroupReference(group=b.group))
        for hs in health.health_status:
            parts = hs.instance.split("/")   # .../zones/<zone>/instances/<name>
            out[f"{parts[-3]}/{parts[-1]}"] = hs.health_state == "HEALTHY"
    return out


def _delete_load_balancer(env: Env, forwarding_rule: str | None) -> None:
    """転送ルール → バックエンドサービス → ヘルスチェック → インスタンスグループ → 内部 IP の順で消す（無いものは飛ばす）。
    forwarding_rule が与えられれば、その参照先（provided の資源）をたどって消す。"""
    compute = _compute()
    names, project, region = _names(env), env.gcp_project, env.gcp_region
    frs, bes, hcs, igs = (compute.ForwardingRulesClient(), compute.RegionBackendServicesClient(),
                          compute.RegionHealthChecksClient(), compute.InstanceGroupsClient())
    be_name, hc_names, groups = names["lb_backend"], [names["lb_health_check"]], []
    fr_name = forwarding_rule or names["lb_forwarding_rule"]
    with contextlib.suppress(_NotFound()):
        fr = frs.get(project=project, region=region, forwarding_rule=fr_name)
        be_name = fr.backend_service.rsplit("/", 1)[-1] or be_name
        frs.delete(project=project, region=region, forwarding_rule=fr_name).result()
        info(f"転送ルール {fr_name} を消しました")
    with contextlib.suppress(_NotFound()):
        be = bes.get(project=project, region=region, backend_service=be_name)
        hc_names = [h.rsplit("/", 1)[-1] for h in be.health_checks] or hc_names
        groups = [b.group for b in be.backends]
        bes.delete(project=project, region=region, backend_service=be_name).result()
        info(f"バックエンドサービス {be_name} を消しました")
    for hc in hc_names:
        with contextlib.suppress(_NotFound()):
            hcs.delete(project=project, region=region, health_check=hc).result()
    if not groups:
        groups = [f"projects/{project}/zones/{z}/instanceGroups/{names['lb_group']}-{z}"
                  for z in sorted({zn.name for zn in compute.ZonesClient().list(project=project)
                                   if zn.region.rsplit("/", 1)[-1] == region})]
    for g in groups:
        zone, name = g.split("/zones/")[1].split("/instanceGroups/")
        with contextlib.suppress(_NotFound()):
            igs.delete(project=project, zone=zone, instance_group=name).result()
    with contextlib.suppress(_NotFound()):
        compute.AddressesClient().delete(project=project, region=region, address=names["lb_address"]).result()
        info(f"ロードバランサの内部 IP {names['lb_address']} を解放しました")


# ---- バケット（1 つ）と静的キー ----
def _storage(env: Env):
    from google.cloud import storage
    return storage.Client(project=env.gcp_project)


def _lifecycle_rules(spec_rules: list[dict]) -> list[dict]:
    """S3 のライフサイクル（render._lifecycle の形）を Cloud Storage の規則に写す。"""
    rules = []
    for r in spec_rules:
        prefix = r.get("Filter", {}).get("Prefix", "")
        if "Expiration" in r:
            cond = {"age": r["Expiration"]["Days"], "isLive": True}
            if prefix:
                cond["matchesPrefix"] = [prefix]
            rules.append({"action": {"type": "Delete"}, "condition": cond})
        if "NoncurrentVersionExpiration" in r:
            cond = {"daysSinceNoncurrentTime": r["NoncurrentVersionExpiration"]["NoncurrentDays"], "isLive": False}
            if prefix:
                cond["matchesPrefix"] = [prefix]
            rules.append({"action": {"type": "Delete"}, "condition": cond})
    return rules


def _canon_rules(rules) -> list[str]:
    return sorted(json.dumps(dict(r), sort_keys=True) for r in rules)


def ensure_bucket(env: Env) -> str:
    """1 つのバケット（オブジェクト保持ロック有効、バージョニング、公開アクセスの防止、ライフサイクル、均一なバケットレベルのアクセス）。
    均一アクセスは IAM の条件（ディレクトリ単位の権限）に必須。registry の S3 ドライバは ACL を付けて均一のバケットに書けないので、
    GCP の registry は GCS ドライバ（サービスアカウントの JSON キー）を使う（配備で判明 2026-10-31）。"""
    from google.cloud import storage
    client = _storage(env)
    name = _names(env)["bucket"]
    bucket = client.lookup_bucket(name)
    if bucket is None:
        bucket = storage.Bucket(client, name=name)
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.iam_configuration.public_access_prevention = "enforced"
        bucket.versioning_enabled = True
        bucket.labels = {LABEL_KEY: env.vm_prefix}
        bucket = client.create_bucket(bucket, location=env.gcp_region, enable_object_retention=True)
        info(f"バケット {name} を作りました（オブジェクト保持ロックあり）")
    if bucket.object_retention_mode != "Enabled":
        raise LabError(f"バケット {name} はオブジェクト保持ロックが無効です（作成時にしか有効にできません。別の名前で作り直すか消してください）")
    changed = False
    if not bucket.iam_configuration.uniform_bucket_level_access_enabled:
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        changed = True
    if bucket.iam_configuration.public_access_prevention != "enforced":
        bucket.iam_configuration.public_access_prevention = "enforced"
        changed = True
    if not bucket.versioning_enabled:
        bucket.versioning_enabled = True
        changed = True
    want = _lifecycle_rules(objstore.vm_lifecycle_rules(env))
    if _canon_rules(bucket.lifecycle_rules) != _canon_rules(want):
        bucket.lifecycle_rules = want
        changed = True
    if changed:
        bucket.patch()
        info(f"バケット {name} の設定を更新しました")
    ok("バケットの設定は最新です")
    return name


def check_bucket(env: Env, name: str, strict: bool) -> None:
    client = _storage(env)
    bucket = client.lookup_bucket(name)
    if bucket is None:
        raise LabError(f"バケット {name}（LAB_VM_BUCKET）がありません")
    problems = []
    if bucket.object_retention_mode != "Enabled":
        problems.append("オブジェクト保持ロックが無効です（作成時に有効にする必要がある）")
    if not bucket.versioning_enabled:
        problems.append("バージョニングが無効です")
    if bucket.iam_configuration.public_access_prevention != "enforced":
        problems.append("公開アクセスの防止が無効です")
    if not bucket.iam_configuration.uniform_bucket_level_access_enabled:
        problems.append("均一なバケットレベルのアクセスが無効です（ディレクトリ単位の権限（IAM の条件）に要る）")
    if _canon_rules(bucket.lifecycle_rules) != _canon_rules(_lifecycle_rules(objstore.vm_lifecycle_rules(env))):
        msg = "ライフサイクルが前提（変更設計書 4.2 節）と違います"
        if strict:
            problems.append(msg)
        else:
            warn(f"バケット {name}: {msg}（用意する側の設定のまま使います）")
    if problems:
        raise LabError(f"バケット {name} が前提と違います:\n  " + "\n  ".join(problems))
    ok(f"バケット {name} は前提どおりです")


def snapshot(env: Env, bucket_name: str) -> list[str]:
    """冪等性の試験用: バケットの設定と IAM のバインディングを文字列の一覧に。"""
    bucket = _storage(env).get_bucket(bucket_name)
    policy = bucket.get_iam_policy(requested_policy_version=3)
    bindings = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in policy.bindings)
    return [f"bucket {bucket_name} uniform={bool(bucket.iam_configuration.uniform_bucket_level_access_enabled)} "
            f"versioning={bool(bucket.versioning_enabled)} retention={bucket.object_retention_mode} "
            f"lifecycle={json.dumps([dict(r) for r in bucket.lifecycle_rules], sort_keys=True)} iam={bindings}"]


def _ensure_service_account(env: Env, account_id: str, display: str) -> str:
    from google.cloud import iam_admin_v1
    client = iam_admin_v1.IAMClient()
    email = _sa_email(env, account_id)
    try:
        client.get_service_account(name=f"projects/{env.gcp_project}/serviceAccounts/{email}")
    except _NotFound():
        client.create_service_account(request={"name": f"projects/{env.gcp_project}", "account_id": account_id,
                                               "service_account": {"display_name": display}})
        info(f"サービスアカウント {email} を作りました")
    return email


def _ensure_custom_role(env: Env, role_name: str, title: str, permissions: list[str]) -> None:
    from google.cloud import iam_admin_v1
    iam = iam_admin_v1.IAMClient()
    try:
        role = iam.get_role(request={"name": role_name})
        if role.deleted:
            # 消したカスタムロールは 7 日間は「削除済み」として残り、同じ名前で作れず、権限も付けられない（配備で判明 2026-10-31）
            role = iam.undelete_role(request={"name": role_name})
            info(f"削除済みのカスタムロール {role_name.rsplit('/', 1)[-1]} を戻しました")
        if sorted(role.included_permissions) != sorted(permissions):
            role.included_permissions = permissions
            iam.update_role(request={"name": role_name, "role": role})
            info(f"カスタムロール {role_name.rsplit('/', 1)[-1]} を更新しました")
    except _NotFound():
        iam.create_role(request={"parent": f"projects/{env.gcp_project}", "role_id": role_name.rsplit("/", 1)[-1],
                                 "role": {"title": title, "included_permissions": permissions, "stage": "GA"}})
        info(f"カスタムロール {role_name.rsplit('/', 1)[-1]} を作りました")


def _set_bucket_iam(env: Env, bucket_name: str, entries: list[dict]) -> None:
    client = _storage(env)
    bucket = client.bucket(bucket_name)
    policy = bucket.get_iam_policy(requested_policy_version=3)
    policy.version = 3
    managed = {f"serviceAccount:{_sa_email(env, f'{env.vm_prefix}-{u}')}" for u in [*STATIC_KEY_USERS, OPERATOR_USER]}
    # 操作者の ADC の主体（./lab が Cloud Storage のクライアントで消す・読むため。均一アクセスのバケットでは project の
    # legacy の権限ではオブジェクトに触れない。配備で判明 2026-10-31）
    adc_member = _adc_member(env)
    if adc_member:
        managed.add(adc_member)
    keep = [b for b in policy.bindings if not (set(b["members"]) & managed)]
    # 同じロール・同じ条件の利用者は 1 つのバインディングにまとめる（GCS は保存時にまとめるので、分けたままだと毎回「違う」になる）
    merged: dict[str, dict] = {}
    for e in entries:
        member = f"serviceAccount:{_sa_email(env, f'{env.vm_prefix}-{e['user']}')}"
        role = _custom_role_name(env, e["role"])
        condition = None
        if e["prefix"]:
            condition = {"title": f"{e['user']}-{e['prefix'].strip('/').replace('/', '-')}",
                         "expression": f'resource.name.startsWith("projects/_/buckets/{bucket_name}/objects/{e["prefix"]}")'}
        key = json.dumps([role, condition], sort_keys=True)
        binding = merged.setdefault(key, {"role": role, "members": set(), **({"condition": condition} if condition else {})})
        binding["members"].add(member)
    if adc_member:
        key = json.dumps(["roles/storage.objectAdmin", None], sort_keys=True)
        merged.setdefault(key, {"role": "roles/storage.objectAdmin", "members": set()})["members"].add(adc_member)
    new = list(merged.values())
    current = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in policy.bindings)
    wanted = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in keep + new)
    if current != wanted:
        policy.bindings = keep + new
        bucket.set_iam_policy(policy)
        info(f"バケット {bucket_name} の権限を更新しました")


REGISTRY_JSON_KEY_ENV = "LAB_VM_GCS_REGISTRY_KEY_JSON"   # registry のサービスアカウントの JSON キー（base64）


def ensure_registry_json_key(env: Env, email: str) -> str | None:
    """registry のサービスアカウントの JSON キー（.env に無ければ作り直す。base64 で返す）。"""
    import base64
    from google.cloud import iam_admin_v1
    if env.get(REGISTRY_JSON_KEY_ENV):
        return None
    iam = iam_admin_v1.IAMClient()
    parent = f"projects/{env.gcp_project}/serviceAccounts/{email}"
    for attempt in range(12):
        try:
            for key in iam.list_service_account_keys(request={"name": parent, "key_types": ["USER_MANAGED"]}).keys:
                iam.delete_service_account_key(request={"name": key.name})
            created = iam.create_service_account_key(request={"name": parent})
            break
        except _NotFound():
            if attempt == 11:
                raise
            time.sleep(5)
    info(f"{email} の JSON キーを作りました（.env の {REGISTRY_JSON_KEY_ENV} に書く）")
    return base64.b64encode(created.private_key_data).decode()


def ensure_static_keys(env: Env, bucket: str) -> dict[str, tuple[str, str]]:
    """役割ごとのサービスアカウント（5 つ ＋ 操作者）と HMAC キー（registry は GCS ドライバ用の JSON キー）。権限は毎回そろえ、
    キーは .env に無い利用者だけ作り直す。戻り値は作ったキー（利用者 → (アクセス ID, シークレット)。registry は ("json", base64)）。"""
    client = _storage(env)
    keys: dict[str, tuple[str, str]] = {}
    for role, (suffix, permissions) in CUSTOM_ROLES.items():
        _ensure_custom_role(env, _custom_role_name(env, role), f"litellm-lab {suffix}", permissions)
    for user in [*STATIC_KEY_USERS, OPERATOR_USER]:
        email = _ensure_service_account(env, f"{env.vm_prefix}-{user}", f"litellm-lab {user} (S3 互換の静的キー)")
        if user == "registry":
            json_key = ensure_registry_json_key(env, email)
            if json_key:
                keys[user] = ("json", json_key)
            continue
        key_name, secret_name = render.s3_env_names(user, env)
        if env.get(key_name) and env.get(secret_name):
            continue
        for attempt in range(12):   # 作った直後のサービスアカウントは数秒〜数十秒は 404 になる（一覧も作成も）
            try:
                for meta in client.list_hmac_keys(service_account_email=email):
                    if meta.state == "ACTIVE":
                        meta.state = "INACTIVE"
                        meta.update()
                    meta.delete()
                meta, secret = client.create_hmac_key(service_account_email=email, project_id=env.gcp_project)
                break
            except _NotFound():
                if attempt == 11:
                    raise
                time.sleep(5)
        keys[user] = (meta.access_id, secret)
        info(f"{email} の HMAC キーを作りました（.env の {key_name} に書く）")
    _set_bucket_iam(env, bucket, gcs_bindings(env, bucket))
    return keys


# ---- VM 3 台（サービスアカウント無し。VM はクラウドの API を呼ばない） ----
def _image(env: Env, existing: list, family: str) -> str:
    """配備済みの台があればそのディスクのイメージ（3 台が同時に置き換わらない）。初回は公式の Ubuntu 24.04 の最新。"""
    compute = _compute()
    for inst in existing:
        for d in inst.disks:
            if d.boot:
                disks = compute.DisksClient()
                zone = inst.zone.rsplit("/", 1)[-1]
                disk = disks.get(project=env.gcp_project, zone=zone, disk=d.source.rsplit("/", 1)[-1])
                if disk.source_image:
                    return disk.source_image
    image = compute.ImagesClient().get_from_family(project=IMAGE_PROJECT, family=family)
    info(f"イメージを {image.name} に決めました（以後の配備もこの値を使う）")
    return image.self_link


def _instances(env: Env, role: str = "node") -> list:
    compute = _compute()
    req = compute.AggregatedListInstancesRequest(project=env.gcp_project,
                                                 filter=f"labels.{LABEL_KEY}={env.vm_prefix} AND labels.lab-role={role}")
    found = []
    for _zone, scoped in compute.InstancesClient().aggregated_list(request=req):
        found.extend(scoped.instances or [])
    return found


def _to_node(inst, index: int | None = None) -> Node:
    zone = inst.zone.rsplit("/", 1)[-1]
    nic = inst.network_interfaces[0] if inst.network_interfaces else None
    public = nic.access_configs[0].nat_i_p if nic and nic.access_configs else ""
    return Node(id=f"{zone}/{inst.name}", index=index if index is not None else int((inst.labels or {}).get("lab-node-index", "0")),
                private_ip=nic.network_i_p if nic else "", public_ip=public or "", zone=zone)


def _create_instance(env: Env, zone: str, name: str, machine_type: str, image: str, disk_type: str, disk_gb: int,
                     subnet: str, private_ip: str, pubkey: str, sa_email: str | None, tags: list[str], labels: dict) -> None:
    compute = _compute()
    inst = compute.Instance(
        name=name, machine_type=f"zones/{zone}/machineTypes/{machine_type}",
        disks=[compute.AttachedDisk(boot=True, auto_delete=True, initialize_params=compute.AttachedDiskInitializeParams(
            source_image=image, disk_size_gb=disk_gb, disk_type=f"zones/{zone}/diskTypes/{disk_type}"))],
        network_interfaces=[compute.NetworkInterface(
            subnetwork=subnet, network_i_p=private_ip,
            access_configs=[compute.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT")])],
        metadata=compute.Metadata(items=[
            compute.Items(key="ssh-keys", value=f"{env.vm_ssh_user}:{pubkey} {env.vm_ssh_user}"),
            compute.Items(key="enable-oslogin", value="FALSE"),
        ]),
        service_accounts=[compute.ServiceAccount(email=sa_email, scopes=[SCOPE])] if sa_email else [],
        tags=compute.Tags(items=tags),
        labels=labels,
    )
    compute.InstancesClient().insert(project=env.gcp_project, zone=zone, instance_resource=inst).result()


def ensure_nodes(env: Env, net: dict, pubkey: str) -> list[Node]:
    """Compute Engine 3 台（別ゾーン、固定の内部 IP、一時的な外部 IP（SSH 用）、Ubuntu 24.04 arm64、Hyperdisk、サービスアカウント無し）。
    無い台だけ作る。初期化は SSH（labtool/vm.py）なので startup-script は無い。"""
    plan, zones, names = net["plan"], net["zones"], _names(env)
    existing = _instances(env)
    by_index = {_to_node(i).index: i for i in existing}
    image = _image(env, existing, IMAGE_FAMILY)
    for n in plan["nodes"]:
        i = n["index"]
        if i in by_index:
            continue
        zone = zones[i - 1]
        name = f"{env.vm_prefix}-node{i}"
        _create_instance(env, zone, name, env.gcp_machine_type, image, DISK_TYPE, env.vm_disk_gib, net["subnet"],
                         n["private_ip"], pubkey, None, [names["tag"]],
                         {LABEL_KEY: env.vm_prefix, "lab-role": "node", "lab-node-index": str(i)})
        info(f"Compute Engine {name}（{zone}、{n['private_ip']}）を起こしました")
    nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    if len(nodes) != NODE_COUNT:
        raise LabError(f"Compute Engine が {len(nodes)} 台です（{NODE_COUNT} 台になるはず）")
    return nodes


def ensure_proxy_node(env: Env, net: dict, pubkey: str) -> Node:
    """中継（出口プロキシ）の VM 1 台（amd64、サブネットの .20、公開 IP あり）。検証では踏み台と Zabbix サーバーも兼ねるので、
    LAB_ZABBIX_VM=true なら e2-medium（4 GiB）とディスク 20 GiB。"""
    plan, zones, names = net["plan"], net["zones"], _names(env)
    existing = _instances(env, role="proxy")
    if not existing:
        image = _image(env, [], PROXY_IMAGE_FAMILY)
        machine, disk = (PROXY_MACHINE_TYPE_ZABBIX, 20) if env.zabbix_vm else (PROXY_MACHINE_TYPE, 10)
        _create_instance(env, zones[0], names["proxy"], machine, image, "pd-balanced", disk, net["subnet"],
                         plan["proxy_ip"], pubkey, None, [names["proxy_tag"]], {LABEL_KEY: env.vm_prefix, "lab-role": "proxy"})
        info(f"中継の VM {names['proxy']}（{zones[0]}、{plan['proxy_ip']}）を起こしました")
        existing = _instances(env, role="proxy")
    return _to_node(existing[0], index=0)


def describe_nodes(env: Env, ids: list[str]) -> list[Node]:
    """.env の識別子（<ゾーン>/<名前>）から Node を組む。順番が台の番号。"""
    compute = _compute()
    nodes = []
    for i, node_id in enumerate(ids, start=1):
        if "/" not in node_id:
            raise LabError(f"LAB_VM_NODES の {node_id} は <ゾーン>/<名前> の形にしてください")
        zone, name = node_id.split("/", 1)
        try:
            inst = compute.InstancesClient().get(project=env.gcp_project, zone=zone, instance=name)
        except _NotFound():
            raise LabError(f"Compute Engine {node_id}（LAB_VM_NODES）がありません")
        nodes.append(_to_node(inst, index=i))
    return nodes


def recreate_node(env: Env, infra: Infra, index: int, pubkey: str) -> Node:
    """1 台を消して作り直す（イメージを新しくするとき。managed だけ）。"""
    instances = _compute().InstancesClient()
    for n in infra.nodes:
        if n.index == index:
            zone, name = n.id.split("/", 1)
            instances.delete(project=env.gcp_project, zone=zone, instance=name).result()
            info(f"Compute Engine {name}（node{index}）を消しました")
    net = ensure_network(env)
    nodes = ensure_nodes(env, net, pubkey)
    _ensure_instance_groups(env, net, nodes)   # 作り直した台をロードバランサの転送先に戻す
    return next(n for n in nodes if n.index == index)


def stop_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().stop(project=env.gcp_project, zone=zone, instance=name).result()


def start_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().start(project=env.gcp_project, zone=zone, instance=name).result()


# ---- provision（managed） ----
def provision(env: Env, pubkey: str, operator_cidr: str) -> dict:
    """作る（無ければ）: ネットワーク → 22 / 中継の許可 → バケット → 静的キー → VM 3 台 → 内部ロードバランサ → 中継（踏み台）の VM。
    戻り値を呼ぶ側が .env に書く（識別子、入口の IP、踏み台、Zabbix の宛先）。"""
    net = ensure_network(env)
    allow_ssh_from(env, operator_cidr)
    if env.egress_proxy_vm:
        allow_proxy_from(env, operator_cidr)
    bucket = ensure_bucket(env)
    keys = ensure_static_keys(env, bucket)
    nodes = ensure_nodes(env, net, pubkey)
    entry_ip, entry_ip_id = ensure_load_balancer(env, net, nodes)
    proxy = ensure_proxy_node(env, net, pubkey) if env.egress_proxy_vm else None
    return {"nodes": [n.id for n in nodes], "bucket": bucket, "entry_ip": entry_ip, "entry_ip_id": entry_ip_id,
            "keys": keys, "proxy": proxy, "proxy_url": f"http://{net['plan']['proxy_ip']}:{PROXY_PORT}" if proxy else "",
            "bastion_host": proxy.public_ip if proxy else "", "bastion_cidr": f"{proxy.private_ip}/32" if proxy else "",
            "zabbix_server": f"{proxy.private_ip}:{ZABBIX_SERVER_PORT}" if proxy and env.zabbix_vm else ""}


# ---- 削除 ----
def _remove_retention_and_delete(bucket, blobs: list) -> None:
    """保持（Unlocked）の付いたオブジェクトは保持を外してから消す。Locked は消せない（警告）。"""
    from google.api_core import exceptions as gexc
    for blob in blobs:
        try:
            if blob.retention and blob.retention.mode:
                if blob.retention.mode == "Locked":
                    warn(f"{blob.name}（世代 {blob.generation}）は保持が Locked なので消せません（期限まで残ります）")
                    continue
                blob.retention.mode = None
                blob.retention.retain_until_time = None
                blob.patch(override_unlocked_retention=True)
            bucket.delete_blob(blob.name, generation=blob.generation)
        except gexc.NotFound:
            pass


def empty_bucket(env: Env, bucket_name: str, prefixes: list[str] | None = None, remove_retention: bool = False) -> int:
    """バケット（または接頭辞の下）を版も含めて空にする。remove_retention なら保持（Unlocked）も外して消す。消した数を返す。"""
    client = _storage(env)
    bucket = client.lookup_bucket(bucket_name)
    if bucket is None:
        return 0
    n = 0
    for prefix in (prefixes or [None]):
        chunk: list = []
        for blob in client.list_blobs(bucket_name, prefix=prefix, versions=True):
            chunk.append(blob)
            if len(chunk) == 100:
                n += _delete_chunk(bucket, chunk, remove_retention)
                chunk = []
        if chunk:
            n += _delete_chunk(bucket, chunk, remove_retention)
    return n


def _delete_chunk(bucket, blobs: list, remove_retention: bool) -> int:
    from google.api_core import exceptions as gexc
    if remove_retention:
        _remove_retention_and_delete(bucket, blobs)
        return len(blobs)
    for attempt in range(6):
        try:
            bucket.delete_blobs(blobs, preserve_generation=True, on_error=lambda b: None)
            return len(blobs)
        except gexc.Forbidden:
            if attempt == 5:
                raise
            time.sleep(10)
    return len(blobs)


def _adc_member(env: Env) -> str | None:
    principal = identity(env)["principal"]
    if "@" not in principal:
        return None
    return ("serviceAccount:" if principal.endswith("gserviceaccount.com") else "user:") + principal


def _grant_operator(env: Env, bucket) -> None:
    """操作者の ADC の主体にバケットの objectAdmin を付ける（消すときに保持を外す・版を消すため。provision の IAM と同じ）。"""
    member = _adc_member(env)
    if member is None:
        return
    policy = bucket.get_iam_policy(requested_policy_version=3)
    policy.version = 3
    if any(b["role"] == "roles/storage.objectAdmin" and not b.get("condition") and member in b["members"] for b in policy.bindings):
        return
    policy.bindings.append({"role": "roles/storage.objectAdmin", "members": {member}})
    bucket.set_iam_policy(policy)
    info(f"バケット {bucket.name} に操作者 {member} の objectAdmin を付けました")
    time.sleep(10)   # IAM の反映待ち


def _delete_bucket(env: Env, name: str) -> None:
    client = _storage(env)
    bucket = client.lookup_bucket(name)
    if bucket is None:
        return
    _grant_operator(env, bucket)
    n = empty_bucket(env, name, remove_retention=True)
    try:
        bucket.delete()
        info(f"バケット {name} を空にして消しました（{n} 版）")
    except Exception as e:  # noqa: BLE001 Locked の保持が残っている
        warn(f"バケット {name} を消せませんでした（{type(e).__name__}）。保持が Locked のオブジェクトが残っているなら期限まで残ります")


def _delete_instances(env: Env, node_ids: list[str]) -> None:
    compute = _compute()
    instances = compute.InstancesClient()
    ops = []
    for node_id in node_ids:
        zone, name = node_id.split("/", 1)
        with contextlib.suppress(_NotFound()):
            ops.append(instances.delete(project=env.gcp_project, zone=zone, instance=name))
    for op in ops:
        op.result()
    if ops:
        info(f"Compute Engine {len(ops)} 台を消しました")


def _delete_address(env: Env, name: str) -> None:
    """前の版が作った静的な外部 IP（浮動 IP）が残っていれば解放する。"""
    compute = _compute()
    try:
        addr = compute.AddressesClient().get(project=env.gcp_project, region=env.gcp_region, address=name)
    except _NotFound():
        return
    compute.AddressesClient().delete(project=env.gcp_project, region=env.gcp_region, address=name).result()
    info(f"静的な外部 IP {addr.address} を解放しました")


def destroy(env: Env) -> None:
    """managed: 名前とラベルで探して全部消す。バケット → HMAC キーとサービスアカウント → ロードバランサ → VM（中継を含む）→
    カスタムロール → ファイアウォール → サブネット → ネットワーク →（前の版の静的 IP）。"""
    from google.cloud import iam_admin_v1
    compute = _compute()
    names = _names(env)
    project = env.gcp_project
    _delete_bucket(env, names["bucket"])
    client = _storage(env)
    iam = iam_admin_v1.IAMClient()
    for user in [*STATIC_KEY_USERS, OPERATOR_USER, "node"]:
        email = _sa_email(env, f"{env.vm_prefix}-{user}")
        with contextlib.suppress(_NotFound()):
            for meta in client.list_hmac_keys(service_account_email=email):
                if meta.state == "ACTIVE":
                    meta.state = "INACTIVE"
                    meta.update()
                meta.delete()
        with contextlib.suppress(_NotFound()):
            iam.delete_service_account(name=f"projects/{project}/serviceAccounts/{email}")
            info(f"サービスアカウント {email} を消しました")
    _delete_load_balancer(env, None)
    ids = [_to_node(i).id for i in _instances(env)] + [_to_node(i).id for i in _instances(env, role="proxy")]
    _delete_instances(env, ids)
    for role_name in [f"projects/{project}/roles/{names['node_role']}",
                      *(_custom_role_name(env, r) for r in CUSTOM_ROLES)]:
        with contextlib.suppress(_NotFound()):
            iam.delete_role(request={"name": role_name})
    fws = compute.FirewallsClient()
    for key in ("fw_web", "fw_internal", "fw_ssh", "fw_proxy", "fw_egress_deny", "fw_egress_internal"):
        with contextlib.suppress(_NotFound()):
            fws.delete(project=project, firewall=names[key]).result()
    with contextlib.suppress(_NotFound()):
        compute.SubnetworksClient().delete(project=project, region=env.gcp_region, subnetwork=names["subnet"]).result()
    with contextlib.suppress(_NotFound()):
        compute.NetworksClient().delete(project=project, network=names["network"]).result()
        info(f"VPC ネットワーク {names['network']} を消しました")
    _delete_address(env, names["address"])


def destroy_provided(env: Env, infra: Infra) -> None:
    """provided の全削除（./lab down --all）: .env の識別子の VM、ロードバランサ（転送ルールからたどる）、バケットを消す。
    静的キー、踏み台、ネットワークは用意する側の物なので消さない。"""
    _delete_bucket(env, infra.bucket)
    if infra.entry_ip_id:
        _delete_load_balancer(env, infra.entry_ip_id)
    _delete_instances(env, [n.id for n in infra.nodes])
