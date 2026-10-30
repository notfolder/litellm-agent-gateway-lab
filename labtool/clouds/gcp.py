"""GCP のクラウド固有層（LAB_TARGET=gcp-vm。変更設計書 .history/2026-10-30-VM環境の共通化とGCP追加/ 5 章）。

Compute Engine 3 台（C4A、Ubuntu 24.04 arm64）、VPC とファイアウォール、静的な外部 IP、Cloud Storage のバケットと
サービスアカウントの HMAC キー。資源は名前（<env.vm_prefix>-…）とラベル lab-prefix で探し、無ければ作る。
資格情報はアプリケーション既定の資格情報（gcloud auth application-default login）。Google のライブラリは関数の中で
読み込む（aws-ec2 だけ使う人の単体試験でも、このモジュールの関数の組を確かめられるように）。
"""

from __future__ import annotations

import contextlib
import json
import time

from .. import objectstore as objstore
from ..core import ROOT, Env, LabError, info, ok, warn
from . import (
    CLUSTER_BUCKET, IMAGES_BUCKET, LOCKED_BUCKETS, NODE_COUNT, STATIC_KEY_USERS, Infra, Node, bucket_specs, network_plan,
    static_key_policies,
)

LABEL_KEY = "lab-prefix"
HOOKS_SCRIPT = ROOT / "vm" / "hooks-gcp.sh"
IMAGE_PROJECT = "ubuntu-os-cloud"
IMAGE_FAMILY = "ubuntu-2404-lts-arm64"
DISK_TYPE = "hyperdisk-balanced"   # C4A は pd-* を使えない
S3_ENDPOINT = "https://storage.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# 操作者が S3 互換の API（boto3）でバケットに触るための HMAC キー（Secret lab-operator-s3 にだけ置く）
OPERATOR_USER = "operator"
# VM のサービスアカウントの権限（浮動 IP の付け替えと、自分の情報の読み取り）
NODE_PERMISSIONS = [
    "compute.instances.get", "compute.instances.list", "compute.instances.addAccessConfig",
    "compute.instances.deleteAccessConfig", "compute.addresses.get", "compute.addresses.list", "compute.addresses.use",
    "compute.zoneOperations.get", "compute.subnetworks.use", "compute.subnetworks.useExternalIp",
]
# S3 の操作 → Cloud Storage の IAM のロール
ROLE_FOR_ACTION = {"s3:PutObject": "roles/storage.objectCreator", "s3:GetObject": "roles/storage.objectViewer",
                   "s3:ListBucket": "roles/storage.objectViewer", "s3:*": "roles/storage.objectAdmin"}


def _compute():
    from google.cloud import compute_v1
    return compute_v1


def _names(env: Env) -> dict[str, str]:
    p = env.vm_prefix
    return {"network": f"{p}-net", "subnet": f"{p}-subnet", "fw_web": f"{p}-web", "fw_internal": f"{p}-internal",
            "fw_ssh": f"{p}-ssh", "tag": f"{p}-node", "address": f"{p}-entry", "node_sa": f"{p}-node",
            "node_role": p.replace("-", "_") + "_node"}


def _sa_email(env: Env, account_id: str) -> str:
    return f"{account_id}@{env.gcp_project}.iam.gserviceaccount.com"


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


def snapshot(env: Env, prefix: str) -> list[str]:
    """冪等性の試験用: バケットの設定（均一アクセス、バージョニング、保持、ライフサイクル）と IAM のバインディングを文字列の一覧に
    （Cloud Storage のクライアントで読む。S3 互換の API では保持と IAM が見えない）。"""
    client = _storage(env)
    lines = []
    for logical in sorted(bucket_specs(env)):
        name = prefix + logical
        bucket = client.get_bucket(name)
        policy = bucket.get_iam_policy(requested_policy_version=3)
        bindings = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in policy.bindings)
        lines.append(f"bucket {name} uniform={bool(bucket.iam_configuration.uniform_bucket_level_access_enabled)} "
                     f"versioning={bool(bucket.versioning_enabled)} retention={bucket.retention_period} "
                     f"lifecycle={json.dumps([dict(r) for r in bucket.lifecycle_rules], sort_keys=True)} iam={bindings}")
    return lines


def bucket_prefix(env: Env, ident: dict) -> str:
    return f"{env.gcp_prefix}-{ident['account']}-"


def s3_endpoint(env: Env) -> str:
    return S3_ENDPOINT


# ---- ネットワーク ----
def ensure_network(env: Env) -> dict:
    """VPC（カスタムモード）、リージョンのサブネット 1 つ（Private Google Access あり）、ファイアウォール 3 つ。"""
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
    wanted = {
        names["fw_web"]: compute.Firewall(
            name=names["fw_web"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=["80", "443"])], source_ranges=["0.0.0.0/0"],
            target_tags=[names["tag"]]),
        names["fw_internal"]: compute.Firewall(
            name=names["fw_internal"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="all")], source_ranges=[plan["subnets"][0]], target_tags=[names["tag"]]),
        # 22 の送信元は allow_ssh_from が置き換える。作るときは誰にも開けない
        names["fw_ssh"]: compute.Firewall(
            name=names["fw_ssh"], network=network.self_link, direction="INGRESS",
            allowed=[compute.Allowed(I_p_protocol="tcp", ports=["22"])], source_ranges=["127.0.0.1/32"],
            target_tags=[names["tag"]]),
    }
    for name, fw in wanted.items():
        if name not in existing:
            fws.insert(project=project, firewall_resource=fw).result()
            info(f"ファイアウォールの規則 {name} を作りました")
    ok("ネットワークは最新です")
    return {"network": network.self_link, "subnet": subnet.self_link, "plan": plan, "zones": _zones(env),
            "tag": names["tag"]}


def allow_ssh_from(env: Env, cidr: str) -> None:
    compute = _compute()
    fws = compute.FirewallsClient()
    name = _names(env)["fw_ssh"]
    try:
        fw = fws.get(project=env.gcp_project, firewall=name)
    except _NotFound():
        return
    if list(fw.source_ranges) == [cidr]:
        return
    fws.patch(project=env.gcp_project, firewall=name, firewall_resource=compute.Firewall(source_ranges=[cidr])).result()
    info(f"22 の許可を {cidr} だけにしました（前: {', '.join(fw.source_ranges) or '無し'}）")


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


def entry_ip_holder(env: Env, infra: Infra) -> str | None:
    """静的 IP を持つ Node.id（<ゾーン>/<名前>）。"""
    addr = _compute().AddressesClient().get(project=env.gcp_project, region=env.gcp_region, address=infra.entry_ip_id)
    for user in addr.users:
        parts = user.split("/")   # .../zones/<zone>/instances/<name>
        return f"{parts[-3]}/{parts[-1]}"
    return None


# ---- バケットと静的キー ----
def _storage(env: Env):
    from google.cloud import storage
    return storage.Client(project=env.gcp_project)


def _lifecycle_rules(spec_rules: list[dict]) -> list[dict]:
    """S3 のライフサイクル（render.s3_buckets の形）を Cloud Storage の規則に写す。"""
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


def ensure_buckets(env: Env, prefix: str) -> dict[str, str]:
    """6 バケット。公開アクセスの防止、均一なバケットレベルのアクセス、バージョニング、保持ポリシー、ライフサイクル。"""
    from google.cloud import storage
    client = _storage(env)
    lock_mode = env.get("LAB_AUDIT_LOCK_MODE", "GOVERNANCE")
    names = {}
    for logical, spec in bucket_specs(env).items():
        name = prefix + logical
        names[logical] = name
        # images だけは「均一なバケットレベルのアクセス」を切る（registry 3.0 の S3 ドライバは毎回 ACL を付け、均一のバケットは
        # どの ACL も InvalidArgument で拒む。配備で判明 2026-10-30、設計書 16 章）。権限は IAM のバインディングで同じに保つ
        uniform = logical != IMAGES_BUCKET
        bucket = client.lookup_bucket(name)
        if bucket is None:
            bucket = storage.Bucket(client, name=name)
            bucket.iam_configuration.uniform_bucket_level_access_enabled = uniform
            bucket.iam_configuration.public_access_prevention = "enforced"
            bucket.labels = {LABEL_KEY: env.vm_prefix}
            bucket = client.create_bucket(bucket, location=env.gcp_region)
            info(f"バケット {name} を作りました")
        changed = False
        if bool(bucket.iam_configuration.uniform_bucket_level_access_enabled) != uniform:
            bucket.iam_configuration.uniform_bucket_level_access_enabled = uniform
            changed = True
        if bucket.iam_configuration.public_access_prevention != "enforced":
            bucket.iam_configuration.public_access_prevention = "enforced"
            changed = True
        if bool(bucket.versioning_enabled) != spec["versioning"]:
            bucket.versioning_enabled = spec["versioning"]
            changed = True
        want_retention = spec["lock"]["Days"] * 86400 if spec["lock"] else None
        if bucket.retention_period != want_retention and not bucket.retention_policy_locked:
            bucket.retention_period = want_retention
            changed = True
        want_rules = _lifecycle_rules(spec["lifecycle"])
        if [dict(r) for r in bucket.lifecycle_rules] != want_rules:
            bucket.lifecycle_rules = want_rules
            changed = True
        if changed:
            bucket.patch()
            info(f"バケット {name} の設定を更新しました")
        if spec["lock"] and lock_mode == "COMPLIANCE" and not bucket.retention_policy_locked:
            bucket.lock_retention_policy()
            warn(f"バケット {name} の保持ポリシーをロックしました（COMPLIANCE。保持が切れるまで誰も消せません）")
    ok("バケットの設定は最新です")
    return names


def check_buckets(env: Env, prefix: str) -> None:
    client = _storage(env)
    problems = []
    for logical, spec in bucket_specs(env).items():
        name = prefix + logical
        bucket = client.lookup_bucket(name)
        if bucket is None:
            problems.append(f"{name}: バケットがありません")
            continue
        if bool(bucket.versioning_enabled) != spec["versioning"]:
            problems.append(f"{name}: バージョニングが {bucket.versioning_enabled}（仕様 {spec['versioning']}）")
        want = spec["lock"]["Days"] * 86400 if spec["lock"] else None
        if bucket.retention_period != want:
            problems.append(f"{name}: 保持ポリシーが {bucket.retention_period}（仕様 {want}）")
        if bucket.iam_configuration.public_access_prevention != "enforced":
            problems.append(f"{name}: 公開アクセスの防止が無効です")
        if [dict(r) for r in bucket.lifecycle_rules] != _lifecycle_rules(spec["lifecycle"]):
            problems.append(f"{name}: ライフサイクルが仕様と違います")
        info(f"{name}: OK")
    if problems:
        raise LabError("Cloud Storage のバケットが仕様と違います:\n  " + "\n  ".join(problems))


def bucket_bindings(env: Env, prefix: str) -> dict[str, list[dict]]:
    """利用者ごとの権限（render.s3_policies ＋ registry）を、バケットごとの IAM の条件付きバインディングに写す。
    戻り値: 実名のバケット → [{"user", "role", "prefix"}]。明示的な拒否は書かない（監査ログの保護は保持ポリシー）。"""
    out: dict[str, list[dict]] = {}
    for user, doc in static_key_policies(env).items():
        for st in doc["Statement"]:
            for arn in st["Resource"]:
                path = arn.removeprefix("arn:aws:s3:::")
                logical, _, obj = path.partition("/")
                obj_prefix = obj.removesuffix("*")
                for action in st["Action"]:
                    if action == "s3:ListBucket" and obj:
                        continue
                    role = ROLE_FOR_ACTION[action]
                    entry = {"user": user, "role": role, "prefix": obj_prefix if obj else ""}
                    if entry not in out.setdefault(prefix + logical, []):
                        out[prefix + logical].append(entry)
    # 操作者（S3 互換の API で初期値を書き、試験で触る）は全バケットの objectAdmin
    for logical in bucket_specs(env):
        out.setdefault(prefix + logical, []).append({"user": OPERATOR_USER, "role": "roles/storage.objectAdmin", "prefix": ""})
    return out


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


def _set_bucket_iam(env: Env, bucket_name: str, entries: list[dict]) -> None:
    client = _storage(env)
    bucket = client.bucket(bucket_name)
    policy = bucket.get_iam_policy(requested_policy_version=3)
    policy.version = 3
    keep = [b for b in policy.bindings if not any(m.startswith(f"serviceAccount:{env.vm_prefix}-") for m in b["members"])]
    # 同じロール・同じ条件の利用者は 1 つのバインディングにまとめる（GCS は保存時にまとめるので、分けたままだと毎回「違う」になる）
    merged: dict[str, dict] = {}
    for e in entries:
        member = f"serviceAccount:{_sa_email(env, f'{env.vm_prefix}-{e['user']}')}"
        condition = None
        if e["prefix"]:
            condition = {"title": f"{e['user']}-{e['prefix'].strip('/').replace('/', '-')}",
                         "expression": f'resource.name.startsWith("projects/_/buckets/{bucket_name}/objects/{e["prefix"]}")'}
        key = json.dumps([e["role"], condition], sort_keys=True)
        binding = merged.setdefault(key, {"role": e["role"], "members": set(), **({"condition": condition} if condition else {})})
        binding["members"].add(member)
    new = list(merged.values())
    current = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in policy.bindings)
    wanted = sorted(json.dumps({**b, "members": sorted(b["members"])}, sort_keys=True) for b in keep + new)
    if current != wanted:
        policy.bindings = keep + new
        bucket.set_iam_policy(policy)
        info(f"バケット {bucket_name} の権限を更新しました")


def ensure_static_keys(env: Env, prefix: str, have: set[str]) -> dict[str, tuple[str, str]]:
    """役割ごとのサービスアカウント（5 つ ＋ 操作者）と HMAC キー。権限は毎回そろえ、キーは have に無い利用者だけ作り直す。"""
    client = _storage(env)
    keys: dict[str, tuple[str, str]] = {}
    for user in [*STATIC_KEY_USERS, OPERATOR_USER]:
        email = _ensure_service_account(env, f"{env.vm_prefix}-{user}", f"litellm-lab {user} (S3 互換の静的キー)")
        if user in have:
            continue
        for meta in client.list_hmac_keys(service_account_email=email):
            if meta.state == "ACTIVE":
                meta.state = "INACTIVE"
                meta.update()
            meta.delete()
        for attempt in range(12):   # 作った直後のサービスアカウントは数秒〜数十秒は 404 になる
            try:
                meta, secret = client.create_hmac_key(service_account_email=email, project_id=env.gcp_project)
                break
            except _NotFound():
                if attempt == 11:
                    raise
                time.sleep(5)
        keys[user] = (meta.access_id, secret)
        info(f"{email} の HMAC キーを作りました（Kubernetes の Secret にだけ置く）")
    for bucket_name, entries in bucket_bindings(env, prefix).items():
        _set_bucket_iam(env, bucket_name, entries)
    return keys


def admin_s3_client(env: Env):
    """操作者の S3 互換のクライアント（HMAC キーは Secret lab-operator-s3 から。vm.load_static_keys が Env に載せる）。"""
    from .. import vm
    vm.load_static_keys(env)
    return objstore.cloud_user_client(env, OPERATOR_USER)


def put_object(env: Env, bucket: str, key: str, body: bytes) -> None:
    _storage(env).bucket(bucket).blob(key).upload_from_string(body)


def get_object(env: Env, bucket: str, key: str) -> bytes | None:
    blob = _storage(env).bucket(bucket).blob(key)
    return blob.download_as_bytes() if blob.exists() else None


# ---- VM 3 台 ----
def _ensure_node_identity(env: Env, prefix: str) -> str:
    """VM のサービスアカウントとカスタムロール（浮動 IP の付け替え）、cluster バケットの k3s/* の読み取り。"""
    from google.cloud import iam_admin_v1, resourcemanager_v3
    names = _names(env)
    email = _ensure_service_account(env, names["node_sa"], "litellm-lab node (VM)")
    iam = iam_admin_v1.IAMClient()
    role_name = f"projects/{env.gcp_project}/roles/{names['node_role']}"
    try:
        role = iam.get_role(request={"name": role_name})
        if sorted(role.included_permissions) != sorted(NODE_PERMISSIONS):
            role.included_permissions = NODE_PERMISSIONS
            iam.update_role(request={"name": role_name, "role": role})
            info(f"カスタムロール {names['node_role']} を更新しました")
    except _NotFound():
        iam.create_role(request={"parent": f"projects/{env.gcp_project}", "role_id": names["node_role"],
                                 "role": {"title": "litellm-lab node", "included_permissions": NODE_PERMISSIONS,
                                          "stage": "GA"}})
        info(f"カスタムロール {names['node_role']} を作りました")
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
    cluster = prefix + CLUSTER_BUCKET
    bucket = _storage(env).bucket(cluster)
    pol = bucket.get_iam_policy(requested_policy_version=3)
    pol.version = 3
    want = {"role": "roles/storage.objectViewer", "members": {member},
            "condition": {"title": "node-k3s", "expression": f'resource.name.startsWith("projects/_/buckets/{cluster}/objects/k3s/")'}}
    if not any(b.get("condition", {}).get("title") == "node-k3s" and member in b["members"] for b in pol.bindings):
        pol.bindings.append(want)
        bucket.set_iam_policy(pol)
    return email


def _image(env: Env, existing: list) -> str:
    """配備済みの台があればそのディスクのイメージ（3 台が同時に置き換わらない）。初回は公式の Ubuntu 24.04 arm64 の最新。"""
    compute = _compute()
    for inst in existing:
        for d in inst.disks:
            if d.boot:
                disks = compute.DisksClient()
                zone = inst.zone.rsplit("/", 1)[-1]
                disk = disks.get(project=env.gcp_project, zone=zone, disk=d.source.rsplit("/", 1)[-1])
                if disk.source_image:
                    return disk.source_image
    image = compute.ImagesClient().get_from_family(project=IMAGE_PROJECT, family=IMAGE_FAMILY)
    info(f"イメージを {image.name} に決めました（以後の配備もこの値を使う）")
    return image.self_link


def _instances(env: Env) -> list:
    compute = _compute()
    req = compute.AggregatedListInstancesRequest(project=env.gcp_project, filter=f"labels.{LABEL_KEY}={env.vm_prefix}")
    found = []
    for _zone, scoped in compute.InstancesClient().aggregated_list(request=req):
        found.extend(scoped.instances or [])
    return found


def _to_node(inst) -> Node:
    zone = inst.zone.rsplit("/", 1)[-1]
    nic = inst.network_interfaces[0] if inst.network_interfaces else None
    public = nic.access_configs[0].nat_i_p if nic and nic.access_configs else ""
    return Node(id=f"{zone}/{inst.name}", index=int((inst.labels or {}).get("lab-node-index", "0")),
                private_ip=nic.network_i_p if nic else "", public_ip=public or "", zone=zone)


def ensure_nodes(env: Env, infra: Infra, user_data) -> list[Node]:
    """Compute Engine 3 台（別ゾーン、固定の内部 IP、一時的な外部 IP、Ubuntu 24.04 arm64、Hyperdisk）。無い台だけ作る。
    初期化のスクリプトは startup-script（毎回の起動で走るので、スクリプト自身が ready なら抜ける）。"""
    compute = _compute()
    net = infra.extra["network"]
    plan, zones, names = net["plan"], net["zones"], _names(env)
    sa_email = _ensure_node_identity(env, infra.bucket_prefix)
    existing = _instances(env)
    by_index = {_to_node(i).index: i for i in existing}
    image = _image(env, existing)
    planned = [Node(id=f"{zones[n['index'] - 1]}/{env.vm_prefix}-node{n['index']}", index=n["index"],
                    private_ip=n["private_ip"], public_ip="", zone=zones[n["index"] - 1]) for n in plan["nodes"]]
    pubkey = infra.extra.get("ssh_pubkey", "")
    instances = compute.InstancesClient()
    for n in plan["nodes"]:
        i = n["index"]
        if i in by_index:
            continue
        zone = zones[i - 1]
        name = f"{env.vm_prefix}-node{i}"
        inst = compute.Instance(
            name=name, machine_type=f"zones/{zone}/machineTypes/{env.gcp_machine_type}",
            disks=[compute.AttachedDisk(boot=True, auto_delete=True, initialize_params=compute.AttachedDiskInitializeParams(
                source_image=image, disk_size_gb=env.vm_disk_gib, disk_type=f"zones/{zone}/diskTypes/{DISK_TYPE}"))],
            network_interfaces=[compute.NetworkInterface(
                subnetwork=net["subnet"], network_i_p=n["private_ip"],
                access_configs=[compute.AccessConfig(name="External NAT", type_="ONE_TO_ONE_NAT")])],
            metadata=compute.Metadata(items=[
                compute.Items(key="startup-script", value=user_data(planned[i - 1], planned)),
                compute.Items(key="ssh-keys", value=f"ubuntu:{pubkey} ubuntu"),
                compute.Items(key="enable-oslogin", value="FALSE"),
            ]),
            service_accounts=[compute.ServiceAccount(email=sa_email, scopes=[SCOPE])],
            tags=compute.Tags(items=[names["tag"]]),
            labels={LABEL_KEY: env.vm_prefix, "lab-node-index": str(i)},
        )
        instances.insert(project=env.gcp_project, zone=zone, instance_resource=inst).result()
        info(f"Compute Engine {name}（{zone}、{n['private_ip']}）を起こしました")
    nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    if len(nodes) != NODE_COUNT:
        raise LabError(f"Compute Engine が {len(nodes)} 台です（{NODE_COUNT} 台になるはず）")
    return nodes


def recreate_node(env: Env, infra: Infra, index: int, user_data) -> Node:
    instances = _compute().InstancesClient()
    for n in infra.nodes:
        if n.index == index:
            zone, name = n.id.split("/", 1)
            instances.delete(project=env.gcp_project, zone=zone, instance=name).result()
            info(f"Compute Engine {name}（node{index}）を消しました")
    nodes = ensure_nodes(env, infra, user_data)
    return next(n for n in nodes if n.index == index)


def discover(env: Env) -> Infra | None:
    compute = _compute()
    names = _names(env)
    project = env.gcp_project
    network = subnet = addr = None
    with contextlib.suppress(_NotFound()):
        network = compute.NetworksClient().get(project=project, network=names["network"])
    with contextlib.suppress(_NotFound()):
        addr = compute.AddressesClient().get(project=project, region=env.gcp_region, address=names["address"])
    if network is None and addr is None:
        return None
    ident = identity(env)
    prefix = bucket_prefix(env, ident)
    infra = Infra(region=env.gcp_region, bucket_prefix=prefix,
                  buckets={logical: prefix + logical for logical in bucket_specs(env)})
    if network is not None:
        infra.network_id = network.self_link
        with contextlib.suppress(_NotFound()):
            subnet = compute.SubnetworksClient().get(project=project, region=env.gcp_region, subnetwork=names["subnet"])
        infra.extra["network"] = {"network": network.self_link, "subnet": subnet.self_link if subnet else "",
                                  "plan": network_plan(env.vm_cidr, subnets_per_node=False), "zones": _zones(env),
                                  "tag": names["tag"]}
    if addr is not None:
        infra.entry_ip, infra.entry_ip_id = addr.address, names["address"]
    infra.nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    return infra


def stop_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().stop(project=env.gcp_project, zone=zone, instance=name).result()


def start_node(env: Env, node: Node) -> None:
    zone, name = node.id.split("/", 1)
    _compute().InstancesClient().start(project=env.gcp_project, zone=zone, instance=name).result()


def node_init_hooks(env: Env, infra: Infra, node: Node) -> str:
    text = HOOKS_SCRIPT.read_text()
    for k, v in {"PROJECT": env.gcp_project, "REGION": env.gcp_region, "CLUSTER_BUCKET": infra.buckets[CLUSTER_BUCKET],
                 "ADDRESS_NAME": infra.entry_ip_id}.items():
        text = text.replace(f"@@{k}@@", v)
    return text


# ---- 削除 ----
def empty_buckets(env: Env, infra: Infra) -> None:
    client = _storage(env)
    for logical, name in sorted(infra.buckets.items()):
        bucket = client.lookup_bucket(name)
        if bucket is None:
            continue
        if bucket.retention_policy_locked or (bucket.retention_period and logical in LOCKED_BUCKETS):
            if bucket.retention_policy_locked:
                warn(f"バケット {name} は保持ポリシーがロックされているので消せません（保持が切れるまで残ります）")
                continue
            # 解除できる保持ポリシー（GOVERNANCE 相当）は外してから消す
            bucket.retention_period = None
            bucket.patch()
        n = 0
        for blob in client.list_blobs(name, versions=True):
            blob.delete(generation=blob.generation)
            n += 1
        bucket.delete()
        info(f"バケット {name} を空にして消しました（{n} 版）")


def destroy(env: Env, keep_entry_ip: bool) -> None:
    """バケット → HMAC キーとサービスアカウント → VM → VM の権限 → ファイアウォール → サブネット → ネットワーク →（静的 IP）。"""
    from google.cloud import iam_admin_v1
    compute = _compute()
    names = _names(env)
    project = env.gcp_project
    infra = discover(env)
    if infra is None:
        ok("GCP に消すものはありません")
        return
    empty_buckets(env, infra)
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
    instances = compute.InstancesClient()
    ops = []
    for n in infra.nodes:
        zone, name = n.id.split("/", 1)
        ops.append(instances.delete(project=project, zone=zone, instance=name))
    for op in ops:
        op.result()
    if ops:
        info(f"Compute Engine {len(ops)} 台を消しました")
    with contextlib.suppress(_NotFound()):
        iam.delete_role(request={"name": f"projects/{project}/roles/{names['node_role']}"})
    fws = compute.FirewallsClient()
    for fw in (names["fw_web"], names["fw_internal"], names["fw_ssh"]):
        with contextlib.suppress(_NotFound()):
            fws.delete(project=project, firewall=fw).result()
    with contextlib.suppress(_NotFound()):
        compute.SubnetworksClient().delete(project=project, region=env.gcp_region, subnetwork=names["subnet"]).result()
    with contextlib.suppress(_NotFound()):
        compute.NetworksClient().delete(project=project, network=names["network"]).result()
        info(f"VPC ネットワーク {names['network']} を消しました")
    if infra.entry_ip_id:
        if keep_entry_ip:
            info(f"静的な外部 IP {infra.entry_ip} は残しました（LAB_VM_KEEP_ENTRY_IP=true。付いていない間も料金が出ます）")
        else:
            compute.AddressesClient().delete(project=project, region=env.gcp_region, address=infra.entry_ip_id).result()
            info(f"静的な外部 IP {infra.entry_ip} を解放しました")
