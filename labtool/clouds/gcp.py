"""GCP のクラウド固有層（LAB_TARGET=gcp-vm。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章）。

provision（managed）: VPC とファイアウォール、静的な外部 IP、Cloud Storage のバケット 1 つ（オブジェクト保持ロック）、
サービスアカウントと HMAC キー、カスタムロール、Compute Engine 3 台（C4A、Ubuntu 24.04 arm64。startup-script は使わず
SSH で初期化する）、中継（出口プロキシ）の VM。資源は名前（<env.vm_prefix>-…）とラベル lab-prefix で探し、無ければ作る。
setup（両モード）: .env の識別子（<ゾーン>/<名前>、アドレスの名前、バケット名）を読む。
資格情報はアプリケーション既定の資格情報（gcloud auth application-default login）。Google のライブラリは関数の中で読み込む。
"""

from __future__ import annotations

import contextlib
import json
import time

from .. import objectstore as objstore
from .. import render
from ..core import ROOT, Env, LabError, info, ok, warn
from . import (
    GCS_ROLE_LISTER, GCS_ROLE_WRITER, NODE_COUNT, OPERATOR_USER, PROXY_PORT, STATIC_KEY_USERS, Infra, Node, gcs_bindings,
    network_plan,
)

LABEL_KEY = "lab-prefix"
HOOKS_SCRIPT = ROOT / "vm" / "hooks-gcp.sh"
IMAGE_PROJECT = "ubuntu-os-cloud"
IMAGE_FAMILY = "ubuntu-2404-lts-arm64"
PROXY_IMAGE_FAMILY = "ubuntu-2404-lts-amd64"
PROXY_MACHINE_TYPE = "e2-small"
DISK_TYPE = "hyperdisk-balanced"   # C4A は pd-* を使えない
S3_ENDPOINT = "https://storage.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# VM のサービスアカウントの権限（浮動 IP の付け替えと、自分の情報の読み取り。バケットの権限は要らない）
NODE_PERMISSIONS = [
    "compute.instances.get", "compute.instances.list", "compute.instances.addAccessConfig",
    "compute.instances.deleteAccessConfig", "compute.addresses.get", "compute.addresses.list", "compute.addresses.use",
    "compute.zoneOperations.get", "compute.subnetworks.use", "compute.subnetworks.useExternalIp",
]
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
    principal = getattr(creds, "service_account_email", None) or getattr(creds, "_account", None) or "(ADC)"
    return {"account": env.gcp_project, "principal": principal, "region": env.gcp_region,
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
    中継の VM を使うときは、3 台の直接の外向き通信を egress の deny で塞ぎ、サブネットの中だけ許す（中継は .20）。"""
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
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=["80", "443"])], source_ranges=["0.0.0.0/0"],
            target_tags=[names["tag"]]),
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
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=[str(PROXY_PORT)])], source_ranges=[cidr],
            target_tags=[names["proxy_tag"]])
        wanted[names["fw_egress_deny"]] = compute.Firewall(
            name=names["fw_egress_deny"], network=network.self_link, direction="EGRESS", priority=1000,
            denied=[compute.Denied(I_p_protocol="all")], destination_ranges=["0.0.0.0/0"], target_tags=[names["tag"]])
        wanted[names["fw_egress_internal"]] = compute.Firewall(
            name=names["fw_egress_internal"], network=network.self_link, direction="EGRESS", priority=900,
            allowed=[compute.Allowed(I_p_protocol="all")], destination_ranges=[cidr], target_tags=[names["tag"]])
    for name, fw in wanted.items():
        if name not in existing:
            fws.insert(project=project, firewall_resource=fw).result()
            info(f"ファイアウォールの規則 {name} を作りました")
    if not env.egress_proxy_vm:
        for name in (names["fw_egress_deny"], names["fw_egress_internal"], names["fw_proxy"]):
            if name in existing:
                fws.delete(project=project, firewall=name).result()
                info(f"ファイアウォールの規則 {name} を消しました（中継の VM を使わない）")
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


def allow_web_from(env: Env, cidr: str) -> None:
    """80/443 の送信元を操作者の IP（LAB_WEB_ALLOW_CIDR か 22 と同じ自動検出）だけにする（ユーザーの判断 2026-10-30: A）。
    Mac が中継を通すときは、中継の VM からの入口への接続も通す（サブネット）。"""
    cidrs = [cidr]
    if env.egress_proxy_vm:
        # 中継の VM から入口（浮動 IP）への接続は、送信元が中継の内部 IP か外部 IP のどちらにも見えうるので両方を許す
        cidrs.append(network_plan(env.vm_cidr, subnets_per_node=False)["subnets"][0])
        for inst in _instances(env, role="proxy"):
            public = _to_node(inst, index=0).public_ip
            if public:
                cidrs.append(f"{public}/32")
    _allow_from(env, "fw_web", cidrs, "80/443")


def allow_proxy_from(env: Env, cidr: str) -> None:
    """中継（3128）の送信元: サブネット ＋ 操作者の IP（Mac も中継を通すため）。"""
    plan = network_plan(env.vm_cidr, subnets_per_node=False)
    _allow_from(env, "fw_proxy", [plan["subnets"][0], cidr], f"中継（{PROXY_PORT}）")


# ---- 浮動 IP ----
def ensure_entry_ip(env: Env) -> tuple[str, str]:
    compute = _compute()
    addrs = compute.AddressesClient()
    name = _names(env)["address"]
    try:
        addr = addrs.get(project=env.gcp_project, region=env.gcp_region, address=name)
    except _NotFound():
        addrs.insert(project=env.gcp_project, region=env.gcp_region, address_resource=compute.Address(
            name=name, address_type="EXTERNAL", network_tier="PREMIUM",
            labels={LABEL_KEY: env.vm_prefix})).result()
        addr = addrs.get(project=env.gcp_project, region=env.gcp_region, address=name)
        info(f"静的な外部 IP {addr.address} を確保しました")
    return addr.address, name


def describe_entry_ip(env: Env, entry_ip_id: str) -> str:
    """浮動 IP の識別子（アドレスの名前）→ IP。"""
    try:
        return _compute().AddressesClient().get(project=env.gcp_project, region=env.gcp_region, address=entry_ip_id).address
    except _NotFound():
        raise LabError(f"静的な外部 IP {entry_ip_id}（LAB_VM_ENTRY_IP_ID）が {env.gcp_region} にありません")


def entry_ip_holder(env: Env, infra: Infra) -> str | None:
    """静的 IP を持つ Node.id（<ゾーン>/<名前>）。"""
    addr = _compute().AddressesClient().get(project=env.gcp_project, region=env.gcp_region, address=infra.entry_ip_id)
    for user in addr.users:
        parts = user.split("/")   # .../zones/<zone>/instances/<name>
        return f"{parts[-3]}/{parts[-1]}"
    return None


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
    principal = identity(env)["principal"]
    adc_member = None
    if "@" in principal:
        adc_member = ("serviceAccount:" if principal.endswith("gserviceaccount.com") else "user:") + principal
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


# ---- VM 3 台 ----
def _ensure_node_identity(env: Env) -> str:
    """VM のサービスアカウントとカスタムロール（浮動 IP の付け替えだけ。バケットの権限は無い）。"""
    from google.cloud import resourcemanager_v3
    names = _names(env)
    email = _ensure_service_account(env, names["node_sa"], "litellm-lab node (VM)")
    role_name = f"projects/{env.gcp_project}/roles/{names['node_role']}"
    _ensure_custom_role(env, role_name, "litellm-lab node", NODE_PERMISSIONS)
    projects = resourcemanager_v3.ProjectsClient()
    resource = f"projects/{env.gcp_project}"
    policy = projects.get_iam_policy(request={"resource": resource})
    member = f"serviceAccount:{email}"
    binding = next((b for b in policy.bindings if b.role == role_name), None)
    if binding is None:
        policy.bindings.add(role=role_name, members=[member])
        projects.set_iam_policy(request={"resource": resource, "policy": policy})
    elif member not in binding.members:
        binding.members.append(member)
        projects.set_iam_policy(request={"resource": resource, "policy": policy})
    return email


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


def ensure_nodes(env: Env, net: dict, sa_email: str, pubkey: str) -> list[Node]:
    """Compute Engine 3 台（別ゾーン、固定の内部 IP、一時的な外部 IP、Ubuntu 24.04 arm64、Hyperdisk）。無い台だけ作る。
    初期化は SSH（labtool/vm.py）なので startup-script は無い。"""
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
                         n["private_ip"], pubkey, sa_email, [names["tag"]],
                         {LABEL_KEY: env.vm_prefix, "lab-role": "node", "lab-node-index": str(i)})
        info(f"Compute Engine {name}（{zone}、{n['private_ip']}）を起こしました")
    nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    if len(nodes) != NODE_COUNT:
        raise LabError(f"Compute Engine が {len(nodes)} 台です（{NODE_COUNT} 台になるはず）")
    return nodes


def ensure_proxy_node(env: Env, net: dict, pubkey: str) -> Node:
    """中継（出口プロキシ）の VM 1 台（amd64 の小型、サブネットの .20、公開 IP あり）。"""
    plan, zones, names = net["plan"], net["zones"], _names(env)
    existing = _instances(env, role="proxy")
    if not existing:
        image = _image(env, [], PROXY_IMAGE_FAMILY)
        _create_instance(env, zones[0], names["proxy"], PROXY_MACHINE_TYPE, image, "pd-balanced", 10, net["subnet"],
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
    nodes = ensure_nodes(env, net, _ensure_node_identity(env), pubkey)
    return next(n for n in nodes if n.index == index)


def stop_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().stop(project=env.gcp_project, zone=zone, instance=name).result()


def start_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().start(project=env.gcp_project, zone=zone, instance=name).result()


def node_init_hooks(env: Env, infra: Infra, node: Node) -> str:
    text = HOOKS_SCRIPT.read_text()
    for k, v in {"PROJECT": env.gcp_project, "REGION": env.gcp_region, "ADDRESS_NAME": infra.entry_ip_id}.items():
        text = text.replace(f"@@{k}@@", v)
    return text


# ---- provision（managed） ----
def provision(env: Env, pubkey: str, operator_cidr: str, web_cidr: str) -> dict:
    """作る（無ければ）: ネットワーク → 22 / 80・443 / 中継の許可 → 浮動 IP → バケット → 静的キー → VM の権限 → VM 3 台 → 中継の VM。
    戻り値を呼ぶ側が .env に書く。"""
    net = ensure_network(env)
    allow_ssh_from(env, operator_cidr)
    allow_web_from(env, web_cidr)
    if env.egress_proxy_vm:
        allow_proxy_from(env, operator_cidr)
    entry_ip, entry_ip_id = ensure_entry_ip(env)
    bucket = ensure_bucket(env)
    keys = ensure_static_keys(env, bucket)
    sa_email = _ensure_node_identity(env)
    nodes = ensure_nodes(env, net, sa_email, pubkey)
    proxy = ensure_proxy_node(env, net, pubkey) if env.egress_proxy_vm else None
    return {"nodes": [n.id for n in nodes], "bucket": bucket, "entry_ip": entry_ip, "entry_ip_id": entry_ip_id,
            "keys": keys, "proxy": proxy, "proxy_url": f"http://{net['plan']['proxy_ip']}:{PROXY_PORT}" if proxy else ""}


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


def _delete_bucket(env: Env, name: str) -> None:
    client = _storage(env)
    bucket = client.lookup_bucket(name)
    if bucket is None:
        return
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


def _delete_address(env: Env, name: str, keep: bool) -> None:
    compute = _compute()
    try:
        addr = compute.AddressesClient().get(project=env.gcp_project, region=env.gcp_region, address=name)
    except _NotFound():
        return
    if keep:
        info(f"静的な外部 IP {addr.address} は残しました（LAB_VM_KEEP_ENTRY_IP=true。付いていない間も料金が出ます）")
        return
    compute.AddressesClient().delete(project=env.gcp_project, region=env.gcp_region, address=name).result()
    info(f"静的な外部 IP {addr.address} を解放しました")


def destroy(env: Env, keep_entry_ip: bool) -> None:
    """managed: 名前とラベルで探して全部消す。バケット → HMAC キーとサービスアカウント → VM（中継を含む）→ カスタムロール →
    ファイアウォール → サブネット → ネットワーク →（静的 IP）。"""
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
    _delete_address(env, names["address"], keep_entry_ip)


def destroy_provided(env: Env, infra: Infra, keep_entry_ip: bool) -> None:
    """provided の全削除（./lab down --all）: .env の識別子の VM、浮動 IP、バケットを消す。
    静的キーとネットワークは用意する側の物なので消さない。"""
    _delete_bucket(env, infra.bucket)
    _delete_instances(env, [n.id for n in infra.nodes])
    if infra.entry_ip_id:
        _delete_address(env, infra.entry_ip_id, keep_entry_ip)
