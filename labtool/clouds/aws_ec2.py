"""AWS のクラウド固有層（LAB_TARGET=aws-ec2。変更設計書 .history/2026-10-30-VM環境の共通化とGCP追加/ 4 章）。

EC2 版の CloudFormation の 4 スタックの中身を boto3 の呼び出しに写したもの。資源はタグ lab:prefix=<env.vm_prefix> で
探し、無ければ作る。消すときも同じタグで選ぶ。資格情報は EKS 版と同じ .env の LAB_AWS_PROFILE（labtool/aws.py）。
"""

from __future__ import annotations

import contextlib
import json
import time

from .. import aws
from .. import objectstore as objstore
from ..core import ROOT, Env, LabError, info, ok, warn
from . import (
    CLUSTER_BUCKET, LOCKED_BUCKETS, NODE_COUNT, STATIC_KEY_USERS, Infra, Node, bucket_specs, network_plan,
    static_key_policies,
)

TAG_KEY = "lab:prefix"
HOOKS_SCRIPT = ROOT / "vm" / "hooks-aws.sh"
UBUNTU_OWNER = "099720109477"   # Canonical
UBUNTU_AMI_PATTERN = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
NODE_STATES = ["pending", "running", "stopping", "stopped"]


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


# ---- 操作者、名前 ----
def identity(env: Env) -> dict:
    ident = aws.identity(env)
    return {"account": ident["Account"], "principal": ident["Arn"], "region": env.aws_region}


def ensure_apis(env: Env) -> None:
    """AWS はサービスの有効化が要らない。"""


def bucket_prefix(env: Env, ident: dict) -> str:
    return f"{env.aws_prefix}-{ident['account']}-"


def s3_endpoint(env: Env) -> str:
    """クラスタの中の S3 互換の設定に渡す接続先。AWS は空（既定のエンドポイント）。"""
    return ""


# ---- ネットワーク ----
def ensure_network(env: Env) -> dict:
    """VPC、公開サブネット 3 つ（別 AZ）、インターネットゲートウェイ、経路、S3 のゲートウェイエンドポイント、
    セキュリティグループ（80/443 は全体、台の間はすべて。22 は allow_ssh_from）。"""
    ec2 = _ec2(env)
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

    sgs = ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": vpc_id}))["SecurityGroups"]
    if sgs:
        sg_id = sgs[0]["GroupId"]
    else:
        sg_id = ec2.create_security_group(
            GroupName=f"{env.vm_prefix}-nodes", VpcId=vpc_id,
            Description="litellm-lab vm nodes: 80/443 from anywhere, 22 from the operator, everything between nodes",
            TagSpecifications=_tag_spec(env, "security-group", f"{env.vm_prefix}-nodes"))["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}]},
            {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": sg_id, "Description": "between nodes"}]},
        ])
        info(f"セキュリティグループ {sg_id} を作りました")
    ok("ネットワークは最新です")
    return {"vpc_id": vpc_id, "subnet_ids": subnet_ids, "security_group_id": sg_id, "plan": plan, "azs": azs}


def allow_ssh_from(env: Env, cidr: str) -> None:
    """22 の送信元を操作者の今の IP（cidr）だけにする。他の送信元の 22 は消す。"""
    ec2 = _ec2(env)
    sgs = ec2.describe_security_groups(Filters=_filters(env))["SecurityGroups"]
    if not sgs:
        return
    sg = sgs[0]
    ssh_rules = [p for p in sg["IpPermissions"] if p.get("FromPort") == 22 and p.get("ToPort") == 22]
    current = {r["CidrIp"] for p in ssh_rules for r in p.get("IpRanges", [])}
    if current == {cidr} and all(not p.get("UserIdGroupPairs") and not p.get("Ipv6Ranges") for p in ssh_rules):
        return
    if ssh_rules:
        ec2.revoke_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=ssh_rules)
    ec2.authorize_security_group_ingress(GroupId=sg["GroupId"], IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": cidr, "Description": "lab-operator"}]}])
    info(f"22 の許可を {cidr} だけにしました" + (f"（前: {', '.join(sorted(current)) or '無し'}）" if current != {cidr} else ""))


# ---- 浮動 IP ----
def ensure_entry_ip(env: Env) -> tuple[str, str]:
    ec2 = _ec2(env)
    addrs = ec2.describe_addresses(Filters=_filters(env))["Addresses"]
    if addrs:
        return addrs[0]["PublicIp"], addrs[0]["AllocationId"]
    res = ec2.allocate_address(Domain="vpc", TagSpecifications=_tag_spec(env, "elastic-ip", f"{env.vm_prefix}-entry"))
    info(f"Elastic IP {res['PublicIp']} を確保しました")
    return res["PublicIp"], res["AllocationId"]


def entry_ip_holder(env: Env, infra: Infra) -> str | None:
    res = _ec2(env).describe_addresses(AllocationIds=[infra.entry_ip_id])
    return (res["Addresses"][0].get("InstanceId") or None) if res["Addresses"] else None


# ---- バケットと静的キー ----
def _s3(env: Env):
    return aws.client(env, "s3")


def ensure_buckets(env: Env, prefix: str) -> dict[str, str]:
    """6 バケット（Mac と同じ 4 つ ＋ images ＋ cluster）。保持・バージョニング・ライフサイクルは objectstore.ensure_buckets
    （Mac の RustFS と同じ関数）で合わせ、公開の遮断を全部に付ける。"""
    s3 = _s3(env)
    names = {logical: prefix + logical for logical in bucket_specs(env)}
    objstore.ensure_buckets(env, s3, specs={names[k]: v for k, v in bucket_specs(env).items()}, region=env.aws_region)
    for name in names.values():
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    return names


def check_buckets(env: Env, prefix: str) -> None:
    objstore.check_buckets(env, _s3(env), specs={prefix + k: v for k, v in bucket_specs(env).items()})


def _prefixed_policy(doc: dict, prefix: str) -> dict:
    def fix(arn: str) -> str:
        return arn.replace("arn:aws:s3:::", f"arn:aws:s3:::{prefix}", 1)
    return {"Version": doc["Version"], "Statement": [
        {**st, "Resource": [fix(r) for r in st["Resource"]]} for st in doc["Statement"]]}


def deny_audit_deletion(prefix: str) -> dict:
    """監査ログの原本と閲覧の記録は、誰も消せず、保持を回避できない（Mac の RustFS と同じ決まり。明示的に拒否）。"""
    arns = []
    for logical in LOCKED_BUCKETS:
        arns += [f"arn:aws:s3:::{prefix}{logical}", f"arn:aws:s3:::{prefix}{logical}/*"]
    return {"Effect": "Deny", "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
                                         "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:DeleteBucket",
                                         "s3:PutBucketObjectLockConfiguration", "s3:PutLifecycleConfiguration"],
            "Resource": arns}


def iam_policies(env: Env, prefix: str) -> dict[str, dict]:
    """IAM ユーザーごとの権限（接頭辞付きのバケット名の ARN ＋ 監査ログの削除の明示的な拒否）。"""
    policies = {}
    for user, doc in static_key_policies(env).items():
        prefixed = _prefixed_policy(doc, prefix)
        prefixed["Statement"].append(deny_audit_deletion(prefix))
        policies[user] = prefixed
    return policies


def _iam_user_name(env: Env, user: str) -> str:
    return f"{env.vm_prefix}-{user}"


def ensure_static_keys(env: Env, prefix: str, have: set[str]) -> dict[str, tuple[str, str]]:
    """役割ごとの IAM ユーザーと静的キー。権限は毎回そろえ、キーは have（Kubernetes の Secret に既にある利用者）に
    無い利用者だけ作り直す（秘密は作った瞬間にしか取れない）。"""
    iam = aws.client(env, "iam")
    keys: dict[str, tuple[str, str]] = {}
    for user, policy in iam_policies(env, prefix).items():
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
        if user in have:
            continue
        for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
        created = iam.create_access_key(UserName=name)["AccessKey"]
        keys[user] = (created["AccessKeyId"], created["SecretAccessKey"])
        info(f"IAM ユーザー {name} の静的キーを作りました（Kubernetes の Secret にだけ置く）")
    return keys


def put_object(env: Env, bucket: str, key: str, body: bytes) -> None:
    _s3(env).put_object(Bucket=bucket, Key=key, Body=body)


def get_object(env: Env, bucket: str, key: str) -> bytes | None:
    s3 = _s3(env)
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None


# ---- VM 3 台 ----
def _node_role_name(env: Env) -> str:
    return f"{env.vm_prefix}-node"


def _ensure_node_role(env: Env, prefix: str, entry_ip_id: str) -> str:
    """インスタンスロール: Elastic IP の付け替えと、cluster バケットの k3s/* の読み取りだけ（Session Manager は無い）。"""
    iam = aws.client(env, "iam")
    name = _node_role_name(env)
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        iam.get_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                        Tags=[{"Key": TAG_KEY, "Value": env.vm_prefix}])
        info(f"インスタンスロール {name} を作りました")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ec2:AssociateAddress", "ec2:DisassociateAddress", "ec2:DescribeAddresses"],
         "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": env.aws_region}}},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{prefix}{CLUSTER_BUCKET}/k3s/*"]},
    ]}
    current = None
    with contextlib.suppress(iam.exceptions.NoSuchEntityException):
        current = iam.get_role_policy(RoleName=name, PolicyName="lab-node")["PolicyDocument"]
    if json.dumps(current, sort_keys=True) != json.dumps(policy, sort_keys=True):
        iam.put_role_policy(RoleName=name, PolicyName="lab-node", PolicyDocument=json.dumps(policy))
    try:
        profile = iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
    except iam.exceptions.NoSuchEntityException:
        profile = iam.create_instance_profile(InstanceProfileName=name, Tags=[{"Key": TAG_KEY, "Value": env.vm_prefix}])[
            "InstanceProfile"]
        time.sleep(10)   # 新しいプロファイルが EC2 に見えるまで
    if not any(r["RoleName"] == name for r in profile.get("Roles", [])):
        iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
        time.sleep(10)
    return name


def resolve_ami(env: Env, existing: list[dict]) -> str:
    """配備済みの台があればその AMI（3 台が同時に置き換わらない）。初回は Canonical の Ubuntu 24.04 arm64 の最新。"""
    for inst in existing:
        if inst.get("ImageId"):
            return inst["ImageId"]
    images = _ec2(env).describe_images(Owners=[UBUNTU_OWNER], Filters=[
        {"Name": "name", "Values": [UBUNTU_AMI_PATTERN]}, {"Name": "state", "Values": ["available"]}])["Images"]
    if not images:
        raise LabError(f"Ubuntu 24.04 arm64 の AMI が {env.aws_region} に見つかりません（{UBUNTU_AMI_PATTERN}）")
    latest = max(images, key=lambda i: i["CreationDate"])
    info(f"AMI を {latest['Name']}（{latest['ImageId']}）に決めました（以後の配備もこの値を使う）")
    return latest["ImageId"]


def _instances(env: Env) -> list[dict]:
    res = _ec2(env).describe_instances(Filters=[{"Name": f"tag:{TAG_KEY}", "Values": [env.vm_prefix]},
                                                {"Name": "instance-state-name", "Values": NODE_STATES}])
    return [i for r in res["Reservations"] for i in r["Instances"]]


def _to_node(inst: dict) -> Node:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    return Node(id=inst["InstanceId"], index=int(tags.get("lab:node-index", "0")), private_ip=inst.get("PrivateIpAddress", ""),
                public_ip=inst.get("PublicIpAddress", ""), zone=inst.get("Placement", {}).get("AvailabilityZone", ""))


def ensure_nodes(env: Env, infra: Infra, user_data) -> list[Node]:
    """EC2 3 台（別サブネット、固定のプライベート IP 2 つずつ、Ubuntu 24.04 arm64、IMDSv2）。無い台だけ作る。
    user_data(node: Node, all_nodes: list[Node]) は VM 共通層の描画関数（vm.render_node_init）。"""
    ec2 = _ec2(env)
    net = infra.extra["network"]
    plan = net["plan"]
    role = _ensure_node_role(env, infra.bucket_prefix, infra.entry_ip_id)
    existing = _instances(env)
    by_index = {_to_node(i).index: i for i in existing}
    ami = resolve_ami(env, existing)
    planned = [Node(id="", index=n["index"], private_ip=n["private_ip"], public_ip="", zone=net["azs"][n["index"] - 1])
               for n in plan["nodes"]]
    for n in plan["nodes"]:
        i = n["index"]
        if i in by_index:
            continue
        res = ec2.run_instances(
            ImageId=ami, InstanceType=env.ec2_instance_type, MinCount=1, MaxCount=1,
            IamInstanceProfile={"Name": role},
            NetworkInterfaces=[{
                "DeviceIndex": 0, "SubnetId": net["subnet_ids"][i - 1], "Groups": [net["security_group_id"]],
                "AssociatePublicIpAddress": True,
                # 2 つ目のプライベート IP に Elastic IP を付ける。1 つ目のパブリック IP は残るので、外向きの通信が途切れない
                "PrivateIpAddresses": [{"PrivateIpAddress": n["private_ip"], "Primary": True},
                                       {"PrivateIpAddress": n["secondary_ip"], "Primary": False}],
            }],
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeType": "gp3", "VolumeSize": env.vm_disk_gib, "Encrypted": True, "DeleteOnTermination": True}}],
            MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
            UserData=user_data(planned[i - 1], planned),
            TagSpecifications=_tag_spec(env, "instance", f"{env.vm_prefix}-node{i}", **{"lab:node-index": str(i)}),
        )
        inst_id = res["Instances"][0]["InstanceId"]
        info(f"EC2 {inst_id}（{env.vm_prefix}-node{i}、{n['private_ip']}）を起こしました")
    ids = [i["InstanceId"] for i in _instances(env)]
    ec2.get_waiter("instance_running").wait(InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    if len(nodes) != NODE_COUNT:
        raise LabError(f"EC2 が {len(nodes)} 台です（{NODE_COUNT} 台になるはず）")
    return nodes


def recreate_node(env: Env, infra: Infra, index: int, user_data) -> Node:
    """1 台を消して作り直す（AMI を新しくするとき）。"""
    ec2 = _ec2(env)
    for n in infra.nodes:
        if n.index == index:
            ec2.terminate_instances(InstanceIds=[n.id])
            ec2.get_waiter("instance_terminated").wait(InstanceIds=[n.id], WaiterConfig={"Delay": 10, "MaxAttempts": 60})
            info(f"EC2 {n.id}（node{index}）を消しました")
    nodes = ensure_nodes(env, infra, user_data)
    return next(n for n in nodes if n.index == index)


def discover(env: Env) -> Infra | None:
    """作ってあるものを API で探す（基盤が無ければ None）。"""
    ec2 = _ec2(env)
    vpcs = ec2.describe_vpcs(Filters=_filters(env))["Vpcs"]
    addrs = ec2.describe_addresses(Filters=_filters(env))["Addresses"]
    if not vpcs and not addrs:
        return None
    ident = identity(env)
    prefix = bucket_prefix(env, ident)
    infra = Infra(region=env.aws_region, bucket_prefix=prefix,
                  buckets={logical: prefix + logical for logical in bucket_specs(env)})
    if vpcs:
        infra.network_id = vpcs[0]["VpcId"]
        sgs = ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": infra.network_id}))["SecurityGroups"]
        subnets = sorted(ec2.describe_subnets(Filters=_filters(env, **{"vpc-id": infra.network_id}))["Subnets"],
                         key=lambda s: s["CidrBlock"])
        infra.extra["network"] = {"vpc_id": infra.network_id, "subnet_ids": [s["SubnetId"] for s in subnets],
                                  "security_group_id": sgs[0]["GroupId"] if sgs else "",
                                  "plan": network_plan(env.vm_cidr, subnets_per_node=True),
                                  "azs": [s["AvailabilityZone"] for s in subnets]}
    if addrs:
        infra.entry_ip, infra.entry_ip_id = addrs[0]["PublicIp"], addrs[0]["AllocationId"]
    infra.nodes = sorted((_to_node(i) for i in _instances(env)), key=lambda n: n.index)
    return infra


def stop_node(env: Env, node: Node) -> None:
    ec2 = _ec2(env)
    ec2.stop_instances(InstanceIds=[node.id])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[node.id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def start_node(env: Env, node: Node) -> None:
    ec2 = _ec2(env)
    ec2.start_instances(InstanceIds=[node.id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[node.id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def node_init_hooks(env: Env, infra: Infra, node: Node) -> str:
    """vm/node-init.sh に差し込むシェルの関数（lab_read_object、lab_self_id、lab_take_entry_ip、lab_ensure_egress）。
    クラウドの値（リージョン、バケット、Elastic IP の割り当て ID、2 つ目の IP）を埋めて返す。"""
    plan = infra.extra["network"]["plan"]
    secondary = next(n["secondary_ip"] for n in plan["nodes"] if n["index"] == node.index)
    text = HOOKS_SCRIPT.read_text()
    for k, v in {"REGION": env.aws_region, "CLUSTER_BUCKET": infra.buckets[CLUSTER_BUCKET],
                 "EIP_ALLOCATION_ID": infra.entry_ip_id, "SECONDARY_IP": secondary,
                 "SUBNET_PREFIX": plan["nodes"][node.index - 1]["subnet"].split("/")[1]}.items():
        text = text.replace(f"@@{k}@@", v)
    return text


# ---- 削除 ----
def empty_buckets(env: Env, infra: Infra) -> None:
    s3 = _s3(env)
    lock_mode = env.get("LAB_AUDIT_LOCK_MODE", "GOVERNANCE")
    for logical, bucket in sorted(infra.buckets.items()):
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            continue
        if lock_mode == "COMPLIANCE" and logical in LOCKED_BUCKETS:
            warn(f"バケット {bucket} は Object Lock（COMPLIANCE）のため消せません。保持期間が切れるまで残ります")
            continue
        n = aws.empty_bucket(s3, bucket)
        info(f"バケット {bucket} を空にしました（{n} 版）")
        with contextlib.suppress(Exception):
            s3.delete_bucket(Bucket=bucket)
            info(f"バケット {bucket} を消しました")


def _delete_legacy_stacks(env: Env) -> None:
    """EC2 版（2026-10-25）の CloudFormation のスタックが残っていれば消す（配備はしていないので普通は無い）。"""
    cf = aws.client(env, "cloudformation")
    for stack in ("nodes", "network", "storage", "eip"):
        name = f"{env.ec2_prefix}-{stack}"
        try:
            cf.describe_stacks(StackName=name)
        except Exception:  # noqa: BLE001 無いスタック
            continue
        info(f"以前の CloudFormation のスタック {name} を消します")
        cf.delete_stack(StackName=name)
        cf.get_waiter("stack_delete_complete").wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})


def destroy(env: Env, keep_entry_ip: bool) -> None:
    """作った順の逆に消す: バケット → IAM ユーザー → EC2 → ロール → セキュリティグループ → サブネット → 経路表 →
    エンドポイント → インターネットゲートウェイ → VPC →（Elastic IP）。"""
    ec2 = _ec2(env)
    iam = aws.client(env, "iam")
    _delete_legacy_stacks(env)
    infra = discover(env)
    if infra is None:
        ok("AWS に消すものはありません")
        return
    empty_buckets(env, infra)
    for user in STATIC_KEY_USERS:
        name = _iam_user_name(env, user)
        with contextlib.suppress(iam.exceptions.NoSuchEntityException):
            for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
                iam.delete_access_key(UserName=name, AccessKeyId=k["AccessKeyId"])
            for pol in iam.list_user_policies(UserName=name)["PolicyNames"]:
                iam.delete_user_policy(UserName=name, PolicyName=pol)
            iam.delete_user(UserName=name)
            info(f"IAM ユーザー {name} を消しました")
    ids = [n.id for n in infra.nodes]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
        info(f"EC2 {len(ids)} 台を消しました")
    role = _node_role_name(env)
    with contextlib.suppress(iam.exceptions.NoSuchEntityException):
        with contextlib.suppress(iam.exceptions.NoSuchEntityException):
            iam.remove_role_from_instance_profile(InstanceProfileName=role, RoleName=role)
        iam.delete_instance_profile(InstanceProfileName=role)
    with contextlib.suppress(iam.exceptions.NoSuchEntityException):
        for pol in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=pol)
        iam.delete_role(RoleName=role)
    if infra.network_id:
        vpc = infra.network_id
        for sg in ec2.describe_security_groups(Filters=_filters(env, **{"vpc-id": vpc}))["SecurityGroups"]:
            _retry(lambda: ec2.delete_security_group(GroupId=sg["GroupId"]), "セキュリティグループの削除")
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
    if infra.entry_ip_id:
        if keep_entry_ip:
            info(f"Elastic IP {infra.entry_ip} は残しました（LAB_VM_KEEP_ENTRY_IP=true。付いていない間も料金が出ます）")
        else:
            ec2.release_address(AllocationId=infra.entry_ip_id)
            info(f"Elastic IP {infra.entry_ip} を解放しました")


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

