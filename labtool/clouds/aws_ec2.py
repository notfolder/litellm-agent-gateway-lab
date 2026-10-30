"""AWS のクラウド固有層（LAB_TARGET=aws-ec2。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章、
.history/2026-10-31-内部LBとVM側の防御とZabbix/ 3.3 節）。

provision（managed）: VPC・サブネット 3（別 AZ）・IGW・経路・S3 のゲートウェイエンドポイント・セキュリティグループ（通す規則だけ。
守りは VM の nftables）、キーペア、内部 NLB（固定の内部 IP、TCP 80/443、ヘルスチェック TCP 80）、S3 のバケット 1 つ
（Object Lock 有効・既定の保持なし）、IAM ユーザー 5 と静的キー、EC2 3 台（Ubuntu 24.04 arm64。インスタンスロール無し。
ユーザーデータは使わず SSH で初期化する）、中継（出口プロキシ。踏み台と Zabbix サーバーも兼ねる）の VM。
資源はタグ lab:prefix=<env.vm_prefix> で探し、無ければ作る。setup（両モード）は .env の識別子（インスタンス ID、NLB の ARN、
バケット名）を読む。資格情報は EKS 版と同じ .env の LAB_AWS_PROFILE（labtool/aws.py）。
EC2 の vCPU 上限で配備できていないため、GCP の層と同じ形で書いてあるが未確認（2026-10-31）。
"""

from __future__ import annotations

import contextlib
import json
import time

from .. import aws
from .. import objectstore as objstore
from .. import render
from ..core import Env, LabError, info, ok, warn
from . import (
    AWS_META_IPS, NODE_COUNT, PROXY_PORT, STATIC_KEY_USERS, ZABBIX_SERVER_PORT, Infra, Node, network_plan,
    s3_user_policies,
)

TAG_KEY = "lab:prefix"
UBUNTU_OWNER = "099720109477"   # Canonical
UBUNTU_AMI_PATTERN = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
PROXY_AMI_PATTERN = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
PROXY_INSTANCE_TYPE = "t3.small"            # 中継だけ
PROXY_INSTANCE_TYPE_ZABBIX = "t3.medium"    # 中継 ＋ Zabbix サーバー（4 GiB）
NODE_STATES = ["pending", "running", "stopping", "stopped"]
# 内部 NLB のヘルスチェック（TCP 80。10 秒 × 2 回で外す）
HC_INTERVAL = 10
HC_THRESHOLD = 2


def _tags(env: Env, name: str, **extra: str) -> list[dict]:
    return [{"Key": "Name", "Value": name}, {"Key": TAG_KEY, "Value": env.vm_prefix},
            *({"Key": k, "Value": v} for k, v in extra.items())]


def _tag_spec(env: Env, resource: str, name: str, **extra: str) -> list[dict]:
    return [{"ResourceType": resource, "Tags": _tags(env, name, **extra)}]


def _filters(env: Env, **extra: str) -> list[dict]:
    return [{"Name": f"tag:{TAG_KEY}", "Values": [env.vm_prefix]},
            *({"Name": k, "Values": [v]} for k, v in extra.items())]


def _ec2(env: Env):
    return aws.client(env, "ec2")


def _s3(env: Env):
    return aws.client(env, "s3")


def _names(env: Env) -> dict[str, str]:
    p = env.vm_prefix
    return {"sg": f"{p}-nodes", "sg_proxy": f"{p}-proxy", "sg_lb": f"{p}-lb", "keypair": f"{p}-operator", "role": f"{p}-node",
            "proxy": f"{p}-proxy", "lb": f"{p}-lb", "tg_https": f"{p}-tg-443", "tg_http": f"{p}-tg-80"}


def _elbv2(env: Env):
    return aws.client(env, "elbv2")


# ---- 操作者、名前 ----
def identity(env: Env) -> dict:
    ident = aws.identity(env)
    return {"account": ident["Account"], "principal": ident["Arn"], "region": env.aws_region}


def ensure_apis(env: Env) -> None:
    """AWS はサービスの有効化が要らない。"""


def s3_endpoint(env: Env) -> str:
    """クラスタの中の S3 互換の設定に渡す接続先。AWS は空（既定のエンドポイント）。"""
    return ""


def _bucket_name(env: Env, ident: dict) -> str:
    return f"{env.aws_prefix}-{ident['account']}-lab"


# ---- ネットワーク ----
def ensure_network(env: Env) -> dict:
    """VPC、公開サブネット 3 つ（別 AZ）、インターネットゲートウェイ、経路、S3 のゲートウェイエンドポイント、
    セキュリティグループ（通す規則だけ: VPC の中は全部、22 は allow_ssh_from が操作者の IP に。守りは VM の nftables）。
    外向きは全開（遮断は nftables。変更設計書 2026-10-31 3.3 節）。"""
    ec2 = _ec2(env)
    names = _names(env)
    plan = network_plan(env.vm_cidr, subnets_per_node=True)
    vpcs = ec2.describe_vpcs(Filters=_filters(env))["Vpcs"]
    if vpcs:
        vpc_id = vpcs[0]["VpcId"]
    else:
        vpc_id = ec2.create_vpc(CidrBlock=plan["vpc"], TagSpecifications=_tag_spec(env, "vpc", env.vm_prefix))["Vpc"]["VpcId"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        for attr in ("EnableDnsSupport", "EnableDnsHostnames"):
            ec2.modify_vpc_attribute(VpcId=vpc_id, **{attr: {"Value": True}})
        info(f"VPC {vpc_id}（{plan['vpc']}）を作りました")

    igws = ec2.describe_internet_gateways(Filters=_filters(env))["InternetGateways"]
    if igws:
        igw_id = igws[0]["InternetGatewayId"]
    else:
        igw_id = ec2.create_internet_gateway(TagSpecifications=_tag_spec(env, "internet-gateway", env.vm_prefix))[
            "InternetGateway"]["InternetGatewayId"]
        info(f"インターネットゲートウェイ {igw_id} を作りました")
    if not any(a["VpcId"] == vpc_id for g in igws for a in g.get("Attachments", [])):
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    rts = ec2.describe_route_tables(Filters=_filters(env, **{"vpc-id": vpc_id}))["RouteTables"]
    if rts:
        rt_id = rts[0]["RouteTableId"]
    else:
        rt_id = ec2.create_route_table(VpcId=vpc_id, TagSpecifications=_tag_spec(env, "route-table", env.vm_prefix))[
            "RouteTable"]["RouteTableId"]
    routes = {r.get("DestinationCidrBlock"): r for r in (rts[0]["Routes"] if rts else [])}
    if "0.0.0.0/0" not in routes:
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)

    service = f"com.amazonaws.{env.aws_region}.s3"
    endpoints = ec2.describe_vpc_endpoints(Filters=_filters(env, **{"vpc-id": vpc_id}))["VpcEndpoints"]
    if not endpoints:
        ec2.create_vpc_endpoint(VpcId=vpc_id, ServiceName=service, VpcEndpointType="Gateway", RouteTableIds=[rt_id],
                                TagSpecifications=_tag_spec(env, "vpc-endpoint", f"{env.vm_prefix}-s3"))
        info("S3 のゲートウェイエンドポイントを作りました")

    azs = [z["ZoneName"] for z in ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}, {"Name": "zone-type", "Values": ["availability-zone"]}])[
        "AvailabilityZones"]][:NODE_COUNT]
    if len(azs) < NODE_COUNT:
        raise LabError(f"{env.aws_region} に使える AZ が {len(azs)} つしかありません（{NODE_COUNT} つ要ります）")
    existing = {s["CidrBlock"]: s for s in ec2.describe_subnets(Filters=_filters(env, **{"vpc-id": vpc_id}))["Subnets"]}
    subnet_ids = []
    for i, cidr in enumerate(plan["subnets"], start=1):
        if cidr in existing:
            subnet_id = existing[cidr]["SubnetId"]
        else:
            subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=azs[i - 1],
                                          TagSpecifications=_tag_spec(env, "subnet", f"{env.vm_prefix}-{i}"))["Subnet"]["SubnetId"]
            ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
            info(f"サブネット {subnet_id}（{cidr}、{azs[i - 1]}）を作りました")
        subnet_ids.append(subnet_id)

    sg_id = _ensure_sg(env, vpc_id, names["sg"], "litellm-lab vm nodes: everything inside the vpc, 22 from the operator")
    _allow_cidr_all(env, sg_id, plan["vpc"])
    _ensure_egress_open(env, sg_id)
    lb_sg_id = _ensure_sg(env, vpc_id, names["sg_lb"], "litellm-lab internal nlb: 80/443 from the vpc")
    _allow_ports(env, names["sg_lb"], [80, 443], [plan["vpc"]], "ロードバランサの 80/443")
    _ensure_egress_open(env, lb_sg_id)
    proxy_sg_id = ""
    if env.egress_proxy_vm:
        proxy_sg_id = _ensure_sg(env, vpc_id, names["sg_proxy"], "litellm-lab egress proxy / bastion: 3128 and 10051 from the vpc")
        _allow_cidr_all(env, proxy_sg_id, plan["vpc"])
        _ensure_egress_open(env, proxy_sg_id)
    ok("ネットワークは最新です")
    return {"vpc_id": vpc_id, "subnet_ids": subnet_ids, "security_group_id": sg_id, "proxy_security_group_id": proxy_sg_id,
            "lb_security_group_id": lb_sg_id, "plan": plan, "azs": azs}


def _allow_cidr_all(env: Env, sg_id: str, cidr: str) -> None:
    """そのグループに「cidr からの全プロトコル」の入りの規則を足す（あれば何もしない）。"""
    ec2 = _ec2(env)
    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    if any(p.get("IpProtocol") == "-1" and any(r.get("CidrIp") == cidr for r in p.get("IpRanges", [])) for p in sg["IpPermissions"]):
        return
    ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": cidr, "Description": "inside the vpc"}]}])


def _ensure_sg(env: Env, vpc_id: str, name: str, description: str) -> str:
    ec2 = _ec2(env)
    sgs = ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": vpc_id, "group-name": name}))["SecurityGroups"]
    if sgs:
        return sgs[0]["GroupId"]
    sg_id = ec2.create_security_group(GroupName=name, VpcId=vpc_id, Description=description,
                                      TagSpecifications=_tag_spec(env, "security-group", name))["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[
        {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "between members"}]}])
    info(f"セキュリティグループ {name}（{sg_id}）を作りました")
    return sg_id


def _egress_rules(env: Env, sg_id: str) -> list[dict]:
    return _ec2(env).describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0].get("IpPermissionsEgress", [])


def _ensure_egress_open(env: Env, sg_id: str) -> None:
    rules = _egress_rules(env, sg_id)
    if any(r.get("IpProtocol") == "-1" and any(x.get("CidrIp") == "0.0.0.0/0" for x in r.get("IpRanges", [])) for r in rules):
        return
    ec2 = _ec2(env)
    if rules:
        ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=rules)
    ec2.authorize_security_group_egress(GroupId=sg_id, IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
    info(f"セキュリティグループ {sg_id} の外向きを全開にしました（遮断は VM の nftables）")


def _allow_ports(env: Env, sg_name: str, ports: list[int], cidrs: list[str], what: str) -> None:
    """ports の送信元を cidrs だけにする（他の送信元は消す）。"""
    ec2 = _ec2(env)
    sgs = ec2.describe_security_groups(Filters=_filters(env, **{"group-name": sg_name}))["SecurityGroups"]
    if not sgs:
        return
    sg = sgs[0]
    rules = [p for p in sg["IpPermissions"] if p.get("FromPort") in ports and p.get("ToPort") == p.get("FromPort")]
    current = {r["CidrIp"] for p in rules for r in p.get("IpRanges", [])}
    covered = {p.get("FromPort") for p in rules}
    if current == set(cidrs) and covered == set(ports) and all(not p.get("UserIdGroupPairs") and not p.get("Ipv6Ranges") for p in rules):
        return
    if rules:
        ec2.revoke_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=rules)
    ec2.authorize_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": c, "Description": "lab"} for c in cidrs]}
        for port in ports])
    info(f"{what} の許可を {', '.join(cidrs)} だけにしました" + (f"（前: {', '.join(sorted(current)) or '無し'}）" if current != set(cidrs) else ""))


def allow_ssh_from(env: Env, cidr: str) -> None:
    """22 の送信元を操作者の今の IP（cidr）だけにする（中継の VM も同じ）。"""
    _allow_ports(env, _names(env)["sg"], [22], [cidr], "22")
    if env.egress_proxy_vm:
        _allow_ports(env, _names(env)["sg_proxy"], [22], [cidr], "中継の 22")


def allow_proxy_from(env: Env, cidr: str) -> None:
    """中継（3128）の送信元に操作者の IP を足す（Mac も中継を通すため。VPC の中は _allow_cidr_all で通してある）。"""
    _allow_ports(env, _names(env)["sg_proxy"], [PROXY_PORT], [cidr], f"中継（{PROXY_PORT}）の操作者")


def health_check_ranges(env: Env) -> list[str]:
    """内部 NLB のヘルスチェックの送信元は VPC の中（NLB のアドレス）。"""
    return [network_plan(env.vm_cidr, subnets_per_node=True)["vpc"]]


def meta_ips(env: Env) -> list[str]:
    """VM が中継を通さず直接届く必要があるアドレス（IMDS、Route 53 Resolver、Amazon Time Sync）。"""
    return list(AWS_META_IPS)


# ---- 内部ロードバランサ（内部 NLB。変更設計書 2026-10-31 3.3 節） ----
def ensure_load_balancer(env: Env, net: dict, nodes: list[Node]) -> tuple[str, str]:
    """内部 NLB（3 サブネットに固定の内部 IP、クロスゾーン）、ターゲットグループ 2 つ（TCP 443 / 80、ヘルスチェック TCP 80）、
    リスナー 2 つ、3 台の登録。(入口の内部 IP（1 番のサブネット）, NLB の ARN) を返す。"""
    elb = _elbv2(env)
    names, plan = _names(env), net["plan"]
    lbs = [lb for lb in elb.describe_load_balancers()["LoadBalancers"] if lb["LoadBalancerName"] == names["lb"]]
    if lbs:
        lb = lbs[0]
    else:
        lb = elb.create_load_balancer(
            Name=names["lb"], Type="network", Scheme="internal", IpAddressType="ipv4",
            SubnetMappings=[{"SubnetId": sid, "PrivateIPv4Address": ip} for sid, ip in zip(net["subnet_ids"], plan["lb_ips"])],
            SecurityGroups=[net["lb_security_group_id"]],
            Tags=_tags(env, names["lb"]))["LoadBalancers"][0]
        elb.modify_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"], Attributes=[
            {"Key": "load_balancing.cross_zone.enabled", "Value": "true"}])
        info(f"内部 NLB {names['lb']}（{', '.join(plan['lb_ips'])}）を作りました")
    tgs = {}
    for key, port in (("tg_https", 443), ("tg_http", 80)):
        found = [tg for tg in elb.describe_target_groups()["TargetGroups"] if tg["TargetGroupName"] == names[key]]
        if found:
            tgs[key] = found[0]
        else:
            tgs[key] = elb.create_target_group(
                Name=names[key], Protocol="TCP", Port=port, VpcId=net["vpc_id"], TargetType="instance",
                HealthCheckProtocol="TCP", HealthCheckPort="80", HealthCheckIntervalSeconds=HC_INTERVAL,
                HealthyThresholdCount=HC_THRESHOLD, UnhealthyThresholdCount=HC_THRESHOLD,
                Tags=_tags(env, names[key]))["TargetGroups"][0]
            info(f"ターゲットグループ {names[key]}（TCP {port}）を作りました")
        registered = {d["Target"]["Id"] for d in elb.describe_target_health(TargetGroupArn=tgs[key]["TargetGroupArn"])[
            "TargetHealthDescriptions"]}
        missing = [n.id for n in nodes if n.id not in registered]
        if missing:
            elb.register_targets(TargetGroupArn=tgs[key]["TargetGroupArn"], Targets=[{"Id": i, "Port": port} for i in missing])
            info(f"ターゲットグループ {names[key]} に {', '.join(missing)} を登録しました")
        listeners = elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]
        if not any(ls["Port"] == port for ls in listeners):
            elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="TCP", Port=port,
                                DefaultActions=[{"Type": "forward", "TargetGroupArn": tgs[key]["TargetGroupArn"]}])
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
    return describe_entry_ip(env, lb["LoadBalancerArn"]), lb["LoadBalancerArn"]


def describe_entry_ip(env: Env, entry_ip_id: str) -> str:
    """入口の識別子（NLB の ARN）→ 内部 IP（AZ の名前順の先頭）。"""
    try:
        lb = _elbv2(env).describe_load_balancers(LoadBalancerArns=[entry_ip_id])["LoadBalancers"][0]
    except Exception as e:  # noqa: BLE001 無い・権限
        raise LabError(f"内部 NLB {entry_ip_id}（LAB_VM_ENTRY_IP_ID）を読めません: {e}") from e
    ips = sorted((az["ZoneName"], a.get("PrivateIPv4Address", "")) for az in lb.get("AvailabilityZones", [])
                 for a in az.get("LoadBalancerAddresses", []))
    ips = [ip for _, ip in ips if ip]
    if not ips:
        raise LabError(f"内部 NLB {entry_ip_id} に内部 IP がありません（作成の途中か、Scheme が internal でない）")
    return ips[0]


def lb_targets_healthy(env: Env, infra: Infra) -> dict[str, bool]:
    """NLB が見る 3 台の健全性（443 のターゲットグループ。Node.id → healthy か）。"""
    elb = _elbv2(env)
    out: dict[str, bool] = {}
    for tg in elb.describe_target_groups(LoadBalancerArn=infra.entry_ip_id)["TargetGroups"]:
        if tg["Port"] != 443:
            continue
        for d in elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]:
            out[d["Target"]["Id"]] = d["TargetHealth"]["State"] == "healthy"
    return out


def _delete_load_balancer(env: Env, lb_arn: str | None) -> None:
    """NLB（リスナーごと）→ ターゲットグループを消す。lb_arn が無ければ名前で探す。"""
    elb = _elbv2(env)
    names = _names(env)
    arns = [lb_arn] if lb_arn else [lb["LoadBalancerArn"] for lb in elb.describe_load_balancers()["LoadBalancers"]
                                     if lb["LoadBalancerName"] == names["lb"]]
    tg_arns = set()
    for arn in arns:
        with contextlib.suppress(Exception):
            for tg in elb.describe_target_groups(LoadBalancerArn=arn)["TargetGroups"]:
                tg_arns.add(tg["TargetGroupArn"])
        with contextlib.suppress(Exception):
            elb.delete_load_balancer(LoadBalancerArn=arn)
            elb.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[arn])
            info(f"内部 NLB {arn.rsplit('/', 2)[-2] if '/' in arn else arn} を消しました")
    for tg in elb.describe_target_groups()["TargetGroups"]:
        if tg["TargetGroupName"] in (names["tg_https"], names["tg_http"]):
            tg_arns.add(tg["TargetGroupArn"])
    for arn in tg_arns:
        _retry(lambda a=arn: elb.delete_target_group(TargetGroupArn=a), "ターゲットグループの削除")


# ---- バケット（1 つ）と静的キー ----
def ensure_bucket(env: Env) -> str:
    name = _bucket_name(env, identity(env))
    objstore.ensure_vm_bucket_s3(env, _s3(env), name, env.aws_region)
    return name


def check_bucket(env: Env, name: str, strict: bool) -> None:
    objstore.check_vm_bucket_s3(env, _s3(env), name, strict)


def snapshot(env: Env, bucket_name: str) -> list[str]:
    return objstore.snapshot_vm_bucket_s3(env, _s3(env), bucket_name)


def empty_bucket(env: Env, bucket_name: str, prefixes: list[str] | None = None, remove_retention: bool = False) -> int:
    """バケット（または接頭辞の下）を版も含めて空にする。remove_retention なら GOVERNANCE の保持を回避して消す。"""
    s3 = _s3(env)
    try:
        s3.head_bucket(Bucket=bucket_name)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for prefix in (prefixes or [""]):
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket_name, Prefix=prefix):
            objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for i in range(0, len(objs), 1000):
                s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objs[i:i + 1000], "Quiet": True},
                                  **({"BypassGovernanceRetention": True} if remove_retention else {}))
            n += len(objs)
    return n


def _iam_user_name(env: Env, user: str) -> str:
    return f"{env.vm_prefix}-{user}"


def ensure_static_keys(env: Env, bucket: str) -> dict[str, tuple[str, str]]:
    """役割ごとの IAM ユーザーと静的キー。権限は毎回そろえ、キーは .env に無い利用者だけ作り直す（秘密は作った瞬間にしか取れない）。"""
    iam = aws.client(env, "iam")
    keys: dict[str, tuple[str, str]] = {}
    for user, policy in s3_user_policies(env, bucket).items():
        name = _iam_user_name(env, user)
        try:
            iam.get_user(UserName=name)
        except iam.exceptions.NoSuchEntityException:
            iam.create_user(UserName=name, Tags=[{"Key": TAG_KEY, "Value": env.vm_prefix}])
            info(f"IAM ユーザー {name} を作りました")
        current = None
        with contextlib.suppress(iam.exceptions.NoSuchEntityException):
            current = iam.get_user_policy(UserName=name, PolicyName="lab")["PolicyDocument"]
        if json.dumps(current, sort_keys=True) != json.dumps(policy, sort_keys=True):
            iam.put_user_policy(UserName=name, PolicyName="lab", PolicyDocument=json.dumps(policy))
            info(f"IAM ユーザー {name} の権限を更新しました")
        key_name, secret_name = render.s3_env_names(user, env)
        if env.get(key_name) and env.get(secret_name):
            continue
        for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
        created = iam.create_access_key(UserName=name)["AccessKey"]
        keys[user] = (created["AccessKeyId"], created["SecretAccessKey"])
        info(f"IAM ユーザー {name} の静的キーを作りました（.env の {key_name} に書く）")
    return keys


# ---- VM 3 台（インスタンスロール無し。VM はクラウドの API を呼ばない） ----
def _ensure_keypair(env: Env, pubkey: str) -> str:
    """構築ツールの SSH の公開鍵を EC2 のキーペアとして取り込む（ユーザーデータを使わないので、cloud-init の鍵の配布に使う）。"""
    ec2 = _ec2(env)
    name = _names(env)["keypair"]
    try:
        ec2.describe_key_pairs(KeyNames=[name])
    except Exception:  # noqa: BLE001 無い
        ec2.import_key_pair(KeyName=name, PublicKeyMaterial=pubkey.encode(),
                            TagSpecifications=_tag_spec(env, "key-pair", name))
        info(f"キーペア {name} を取り込みました")
    return name


def resolve_ami(env: Env, existing: list[dict], pattern: str = UBUNTU_AMI_PATTERN) -> str:
    """配備済みの台があればその AMI（3 台が同時に置き換わらない）。初回は Canonical の Ubuntu 24.04 の最新。"""
    for inst in existing:
        if inst.get("ImageId"):
            return inst["ImageId"]
    images = _ec2(env).describe_images(Owners=[UBUNTU_OWNER], Filters=[
        {"Name": "name", "Values": [pattern]}, {"Name": "state", "Values": ["available"]}])["Images"]
    if not images:
        raise LabError(f"Ubuntu 24.04 の AMI が {env.aws_region} に見つかりません（{pattern}）")
    latest = max(images, key=lambda i: i["CreationDate"])
    info(f"AMI を {latest['Name']}（{latest['ImageId']}）に決めました（以後の配備もこの値を使う）")
    return latest["ImageId"]


def _instances(env: Env, role: str = "node") -> list[dict]:
    res = _ec2(env).describe_instances(Filters=[{"Name": f"tag:{TAG_KEY}", "Values": [env.vm_prefix]},
                                                {"Name": "tag:lab:role", "Values": [role]},
                                                {"Name": "instance-state-name", "Values": NODE_STATES}])
    return [i for r in res["Reservations"] for i in r["Instances"]]


def _to_node(inst: dict, index: int | None = None) -> Node:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    return Node(id=inst["InstanceId"], index=index if index is not None else int(tags.get("lab:node-index", "0")),
                private_ip=inst.get("PrivateIpAddress", ""), public_ip=inst.get("PublicIpAddress", ""),
                zone=inst.get("Placement", {}).get("AvailabilityZone", ""))


def ensure_nodes(env: Env, net: dict, pubkey: str) -> list[Node]:
    """EC2 3 台（別サブネット、固定のプライベート IP、公開 IP（SSH 用）、Ubuntu 24.04 arm64、IMDSv2、キーペア、ロール無し）。
    無い台だけ作る。"""
    ec2 = _ec2(env)
    plan = net["plan"]
    keypair = _ensure_keypair(env, pubkey)
    existing = _instances(env)
    by_index = {_to_node(i).index: i for i in existing}
    ami = resolve_ami(env, existing)
    for n in plan["nodes"]:
        i = n["index"]
        if i in by_index:
            continue
        res = ec2.run_instances(
            ImageId=ami, InstanceType=env.ec2_instance_type, MinCount=1, MaxCount=1, KeyName=keypair,
            NetworkInterfaces=[{
                "DeviceIndex": 0, "SubnetId": net["subnet_ids"][i - 1], "Groups": [net["security_group_id"]],
                "AssociatePublicIpAddress": True, "PrivateIpAddress": n["private_ip"],
            }],
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeType": "gp3", "VolumeSize": env.vm_disk_gib, "Encrypted": True, "DeleteOnTermination": True}}],
            MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
            TagSpecifications=_tag_spec(env, "instance", f"{env.vm_prefix}-node{i}",
                                        **{"lab:node-index": str(i), "lab:role": "node"}),
        )
        info(f"EC2 {res['Instances'][0]['InstanceId']}（{env.vm_prefix}-node{i}、{n['private_ip']}）を起こしました")
    ids = [i["InstanceId"] for i in _instances(env)]
    ec2.get_waiter("instance_running").wait(InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    if len(nodes) != NODE_COUNT:
        raise LabError(f"EC2 が {len(nodes)} 台です（{NODE_COUNT} 台になるはず）")
    return nodes


def ensure_proxy_node(env: Env, net: dict, pubkey: str) -> Node:
    """中継（出口プロキシ）の VM 1 台（amd64、1 番のサブネットの .20、公開 IP あり）。検証では踏み台と Zabbix サーバーも兼ねるので、
    LAB_ZABBIX_VM=true なら t3.medium（4 GiB）とディスク 20 GiB。"""
    ec2 = _ec2(env)
    existing = _instances(env, role="proxy")
    if not existing:
        ami = resolve_ami(env, [], PROXY_AMI_PATTERN)
        itype, disk = (PROXY_INSTANCE_TYPE_ZABBIX, 20) if env.zabbix_vm else (PROXY_INSTANCE_TYPE, 10)
        res = ec2.run_instances(
            ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1, KeyName=_ensure_keypair(env, pubkey),
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": net["subnet_ids"][0], "Groups": [net["proxy_security_group_id"]],
                                "AssociatePublicIpAddress": True, "PrivateIpAddress": net["plan"]["proxy_ip"]}],
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeType": "gp3", "VolumeSize": disk, "DeleteOnTermination": True}}],
            MetadataOptions={"HttpTokens": "required"},
            TagSpecifications=_tag_spec(env, "instance", _names(env)["proxy"], **{"lab:role": "proxy"}))
        info(f"中継の VM {res['Instances'][0]['InstanceId']}（{net['plan']['proxy_ip']}）を起こしました")
        existing = _instances(env, role="proxy")
    ec2.get_waiter("instance_running").wait(InstanceIds=[existing[0]["InstanceId"]], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    return _to_node(_instances(env, role="proxy")[0], index=0)


def describe_nodes(env: Env, ids: list[str]) -> list[Node]:
    """.env の識別子（インスタンス ID）から Node を組む。順番が台の番号。"""
    res = _ec2(env).describe_instances(InstanceIds=ids)
    found = {i["InstanceId"]: i for r in res["Reservations"] for i in r["Instances"]}
    nodes = []
    for i, node_id in enumerate(ids, start=1):
        if node_id not in found:
            raise LabError(f"EC2 {node_id}（LAB_VM_NODES）がありません")
        nodes.append(_to_node(found[node_id], index=i))
    return nodes


def recreate_node(env: Env, infra: Infra, index: int, pubkey: str) -> Node:
    """1 台を消して作り直す（AMI を新しくするとき。managed だけ）。"""
    ec2 = _ec2(env)
    for n in infra.nodes:
        if n.index == index:
            ec2.terminate_instances(InstanceIds=[n.id])
            ec2.get_waiter("instance_terminated").wait(InstanceIds=[n.id], WaiterConfig={"Delay": 10, "MaxAttempts": 60})
            info(f"EC2 {n.id}（node{index}）を消しました")
    net = ensure_network(env)
    nodes = ensure_nodes(env, net, pubkey)
    ensure_load_balancer(env, net, nodes)   # 作り直した台をターゲットに戻す
    return next(n for n in nodes if n.index == index)


def stop_node(env: Env, node: Node) -> None:
    ec2 = _ec2(env)
    ec2.stop_instances(InstanceIds=[node.id])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[node.id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def start_node(env: Env, node: Node) -> None:
    ec2 = _ec2(env)
    ec2.start_instances(InstanceIds=[node.id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[node.id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


# ---- provision（managed） ----
def provision(env: Env, pubkey: str, operator_cidr: str) -> dict:
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
def _delete_bucket(env: Env, name: str) -> None:
    s3 = _s3(env)
    try:
        s3.head_bucket(Bucket=name)
    except Exception:  # noqa: BLE001
        return
    n = empty_bucket(env, name, remove_retention=True)
    try:
        s3.delete_bucket(Bucket=name)
        info(f"バケット {name} を空にして消しました（{n} 版）")
    except Exception as e:  # noqa: BLE001 COMPLIANCE の保持が残っている
        warn(f"バケット {name} を消せませんでした（{type(e).__name__}）。COMPLIANCE の保持が切れるまで残ります")


def _terminate(env: Env, ids: list[str]) -> None:
    if not ids:
        return
    ec2 = _ec2(env)
    ec2.terminate_instances(InstanceIds=ids)
    ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
    info(f"EC2 {len(ids)} 台を消しました")


def _release(env: Env, allocation_id: str) -> None:
    """前の版が確保した Elastic IP（浮動 IP）が残っていれば解放する。"""
    ec2 = _ec2(env)
    res = ec2.describe_addresses(AllocationIds=[allocation_id])["Addresses"] if allocation_id else []
    if not res:
        return
    ec2.release_address(AllocationId=allocation_id)
    info(f"Elastic IP {res[0]['PublicIp']} を解放しました")


def destroy(env: Env) -> None:
    """managed: タグで探して全部消す。バケット → IAM ユーザー → 内部 NLB → EC2（中継を含む）→（前の版のロール）→ キーペア →
    セキュリティグループ → サブネット → 経路表 → エンドポイント → インターネットゲートウェイ → VPC →（前の版の Elastic IP）。"""
    ec2 = _ec2(env)
    iam = aws.client(env, "iam")
    names = _names(env)
    _delete_bucket(env, _bucket_name(env, identity(env)))
    for user in STATIC_KEY_USERS:
        name = _iam_user_name(env, user)
        with contextlib.suppress(iam.exceptions.NoSuchEntityException):
            for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
                iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
            for pol in iam.list_user_policies(UserName=name)["PolicyNames"]:
                iam.delete_user_policy(UserName=name, PolicyName=pol)
            iam.delete_user(UserName=name)
            info(f"IAM ユーザー {name} を消しました")
    _delete_load_balancer(env, None)
    _terminate(env, [i["InstanceId"] for i in _instances(env)] + [i["InstanceId"] for i in _instances(env, role="proxy")])
    role = names["role"]
    with contextlib.suppress(iam.exceptions.NoSuchEntityException):
        with contextlib.suppress(iam.exceptions.NoSuchEntityException):
            iam.remove_role_from_instance_profile(InstanceProfileName=role, RoleName=role)
        iam.delete_instance_profile(InstanceProfileName=role)
    with contextlib.suppress(iam.exceptions.NoSuchEntityException):
        for pol in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=pol)
        iam.delete_role(RoleName=role)
    with contextlib.suppress(Exception):
        ec2.delete_key_pair(KeyName=names["keypair"])
    vpcs = ec2.describe_vpcs(Filters=_filters(env))["Vpcs"]
    for vpc in [v["VpcId"] for v in vpcs]:
        for sg in ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": vpc}))["SecurityGroups"]:
            # 互いを参照する規則を先に外す
            if sg["IpPermissions"]:
                ec2.revoke_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"])
            if sg.get("IpPermissionsEgress"):
                ec2.revoke_security_group_egress(GroupId=sg["GroupId"], IpPermissions=sg["IpPermissionsEgress"])
        for sg in ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": vpc}))["SecurityGroups"]:
            _retry(lambda sg_id=sg["GroupId"]: ec2.delete_security_group(GroupId=sg_id), "セキュリティグループの削除")
        for sn in ec2.describe_subnets(Filters=_filters(env, **{"vpc-id": vpc}))["Subnets"]:
            ec2.delete_subnet(SubnetId=sn["SubnetId"])
        for ep in ec2.describe_vpc_endpoints(Filters=_filters(env, **{"vpc-id": vpc}))["VpcEndpoints"]:
            ec2.delete_vpc_endpoints(VpcEndpointIds=[ep["VpcEndpointId"]])
        for rt in ec2.describe_route_tables(Filters=_filters(env, **{"vpc-id": vpc}))["RouteTables"]:
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main"):
                    ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
        for igw in ec2.describe_internet_gateways(Filters=_filters(env))["InternetGateways"]:
            for a in igw.get("Attachments", []):
                ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=a["VpcId"])
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
        _retry(lambda: ec2.delete_vpc(VpcId=vpc), "VPC の削除")
        info(f"VPC {vpc} とネットワークを消しました")
    for addr in ec2.describe_addresses(Filters=_filters(env))["Addresses"]:
        _release(env, addr["AllocationId"])
    if not vpcs:
        ok("AWS に消すネットワークはありません")


def destroy_provided(env: Env, infra: Infra) -> None:
    """provided の全削除（./lab down --all）: .env の識別子の VM、内部 NLB（ARN）、バケットを消す。
    IAM ユーザー、踏み台、ネットワークは用意する側の物なので消さない。"""
    _delete_bucket(env, infra.bucket)
    if infra.entry_ip_id:
        _delete_load_balancer(env, infra.entry_ip_id)
    _terminate(env, [n.id for n in infra.nodes])


def _retry(fn, what: str, attempts: int = 12, delay: int = 10) -> None:
    """依存するものが消えるのを待ちながら試す（セキュリティグループと VPC は、ENI が消えるまで失敗する）。"""
    for i in range(attempts):
        try:
            fn()
            return
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1:
                raise LabError(f"{what}が終わりませんでした: {e}") from e
            time.sleep(delay)
