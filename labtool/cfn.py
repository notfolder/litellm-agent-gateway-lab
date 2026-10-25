"""CloudFormation のテンプレート（LAB_TARGET=aws-ec2。変更設計書 .history/2026-10-25-EC2三台構成/ 3 章）。

副作用のない関数だけ（単体テストの対象）。テンプレートは Python の辞書で組み、JSON で書き出す
（YAML の !Ref などの独自タグを扱わずに済み、S3 の定義（render.s3_buckets、render.s3_policies）を
そのまま使える）。スタックは 4 つ: eip、storage、network、nodes。
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from . import render
from .core import K3S_IMAGE, ROOT, Env

STACKS = ["eip", "storage", "network", "nodes"]
NODE_COUNT = 3
NODE_INIT_SCRIPT = ROOT / "cfn" / "node-init.sh"
# クラスタの中のイメージ置き場（registry。保存先は S3）。ノードは自分の NodePort 経由で引く（node-init.sh）
REGISTRY_HOST = "lab-registry:5000"
REGISTRY_NODEPORT = 30500
IMAGES_BUCKET = "images"
# S3 の利用者（Mac の RustFS と同じ 4 つ）に、イメージ置き場の利用者を足す。Bedrock は S3 ではないので別
REGISTRY_USER = "registry"
BEDROCK_USER = "litellm-bedrock"
IAM_USERS = [*render.S3_USERS, REGISTRY_USER, BEDROCK_USER]
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
# keepalived の優先度（1 番が高い。健全性の検査に失敗すると 100 下がる）
PRIORITIES = [200, 150, 100]


def camel(name: str) -> str:
    """論理 ID 用（例 audit-raw → AuditRaw）。"""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def output_key(user: str, kind: str) -> str:
    """IAM ユーザーのアクセスキーの出力名（kind は AccessKeyId か SecretAccessKey）。"""
    return f"{camel(user)}{kind}"


def k3s_install_version(image: str = K3S_IMAGE) -> str:
    """Mac の k3s のイメージのタグ（v1.35.5-k3s1）を、インストールスクリプトの版の形（v1.35.5+k3s1）にする。"""
    return image.rsplit(":", 1)[1].replace("-k3s", "+k3s")


def network_plan(cidr: str) -> dict:
    """VPC の CIDR（/16）から、3 つの公開サブネット（先頭の /24 × 3）と各台の固定 IP を決める。
    1 つ目のプライベート IP は .10、Elastic IP を付ける 2 つ目は .11。"""
    net = ipaddress.ip_network(cidr)
    subnets = list(net.subnets(new_prefix=24))[:NODE_COUNT]
    nodes = []
    for i, sn in enumerate(subnets, start=1):
        hosts = list(sn.hosts())
        nodes.append({"index": i, "subnet": str(sn), "private_ip": str(hosts[9]), "secondary_ip": str(hosts[10])})
    return {"vpc": str(net), "subnets": [str(s) for s in subnets], "nodes": nodes}


# ---- eip ----
def eip_template(env: Env) -> dict:
    """入口の Elastic IP。別スタックにして、ほかを作り直しても IP（DNS の A レコード）が変わらないようにする。"""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"{env.aws_prefix}: Elastic IP for the entry point (kept across rebuilds)",
        "Resources": {"EntryIp": {"Type": "AWS::EC2::EIP", "Properties": {
            "Domain": "vpc", "Tags": [{"Key": "Name", "Value": f"{env.ec2_prefix}-entry"}]}}},
        "Outputs": {
            "EipAllocationId": {"Value": {"Fn::GetAtt": ["EntryIp", "AllocationId"]}},
            "EipPublicIp": {"Value": {"Ref": "EntryIp"}},
        },
    }


# ---- storage（S3 と IAM ユーザー） ----
def _lifecycle_rule(rule: dict) -> dict:
    out: dict = {"Id": rule["ID"], "Status": rule["Status"]}
    prefix = (rule.get("Filter") or {}).get("Prefix", "")
    if prefix:
        out["Prefix"] = prefix
    if "Expiration" in rule:
        out["ExpirationInDays"] = rule["Expiration"]["Days"]
    if "NoncurrentVersionExpiration" in rule:
        out["NoncurrentVersionExpiration"] = {"NoncurrentDays": rule["NoncurrentVersionExpiration"]["NoncurrentDays"]}
    return out


def _bucket(name: str, spec: dict) -> dict:
    props: dict = {
        "BucketName": name,
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    }
    if spec["versioning"]:
        props["VersioningConfiguration"] = {"Status": "Enabled"}
    if spec["lock"]:
        props["ObjectLockEnabled"] = True
        props["ObjectLockConfiguration"] = {"ObjectLockEnabled": "Enabled",
                                            "Rule": {"DefaultRetention": dict(spec["lock"])}}
    if spec["lifecycle"]:
        props["LifecycleConfiguration"] = {"Rules": [_lifecycle_rule(r) for r in spec["lifecycle"]]}
    # 消すときは labtool が先に空にする（Object Lock のバケットは保持を越えて）。消せない COMPLIANCE は残して知らせる
    return {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete",
            "Properties": props}


def _prefixed_policy(doc: dict, bucket_prefix: str) -> dict:
    """RustFS 用の権限（論理名のバケット）を、接頭辞付きの実際のバケット名の ARN に写す。"""
    def fix(arn: str) -> str:
        return arn.replace("arn:aws:s3:::", f"arn:aws:s3:::{bucket_prefix}", 1)

    return {"Version": doc["Version"], "Statement": [
        {**st, "Resource": [fix(r) for r in st["Resource"]]} for st in doc["Statement"]]}


def _deny_audit_deletion(bucket_prefix: str) -> dict:
    """監査ログの原本と閲覧の記録は、誰も消せず、保持を回避できない（Mac の RustFS と同じ決まり。明示的に拒否）。"""
    arns = []
    for logical in (render.AUDIT_RAW, render.AUDIT_ACCESS):
        arns += [f"arn:aws:s3:::{bucket_prefix}{logical}", f"arn:aws:s3:::{bucket_prefix}{logical}/*"]
    return {"Effect": "Deny", "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
                                         "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:DeleteBucket",
                                         "s3:PutBucketObjectLockConfiguration", "s3:PutLifecycleConfiguration"],
            "Resource": arns}


def iam_policies(env: Env, bucket_prefix: str) -> dict[str, dict]:
    """IAM ユーザーごとの権限。S3 の 4 利用者は render.s3_policies（Mac と同じ）＋ 監査ログの削除の明示的な拒否。
    registry はイメージ置き場のバケットだけ。litellm-bedrock は Bedrock の呼び出しだけ。"""
    policies = {}
    for user, doc in render.s3_policies().items():
        prefixed = _prefixed_policy(doc, bucket_prefix)
        prefixed["Statement"].append(_deny_audit_deletion(bucket_prefix))
        policies[user] = prefixed
    images = f"arn:aws:s3:::{bucket_prefix}{IMAGES_BUCKET}"
    policies[REGISTRY_USER] = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"], "Resource": [images, images + "/*"]},
        _deny_audit_deletion(bucket_prefix)]}
    policies[BEDROCK_USER] = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        # 日本内のクロスリージョン推論プロファイルは、プロファイルと、転送先リージョンの基盤モデルの両方の許可が要る
        "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                     {"Fn::Sub": "arn:aws:bedrock:*:${AWS::AccountId}:inference-profile/*"}]}]}
    return policies


def storage_template(env: Env, account: str) -> dict:
    """S3 の 5 バケット（Mac と同じ 4 つ ＋ images）と、役割ごとの IAM ユーザーと静的キー。
    秘密のほうのキーはスタックの出力で受け取る（CloudFormation を読める人に見える。管理者 1 人の検証環境で許容）。"""
    prefix = render.bucket_prefix(env, account)
    buckets = dict(render.s3_buckets(env))
    buckets[IMAGES_BUCKET] = {"lock": None, "versioning": False, "lifecycle": []}
    resources: dict = {}
    outputs: dict = {"BucketPrefix": {"Value": prefix}}
    for logical, spec in buckets.items():
        resources[f"{camel(logical)}Bucket"] = _bucket(prefix + logical, spec)
        outputs[f"{camel(logical)}BucketName"] = {"Value": {"Ref": f"{camel(logical)}Bucket"}}
    # images: 途中で止まった分割アップロードだけ 7 日で消す（イメージ自体は registry が管理する）
    resources[f"{camel(IMAGES_BUCKET)}Bucket"]["Properties"]["LifecycleConfiguration"] = {"Rules": [
        {"Id": "abandoned-uploads", "Status": "Enabled", "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}
    for user, doc in iam_policies(env, prefix).items():
        uid = f"{camel(user)}User"
        resources[uid] = {"Type": "AWS::IAM::User", "Properties": {
            "UserName": f"{env.ec2_prefix}-{user}",
            "Policies": [{"PolicyName": f"lab-{user}", "PolicyDocument": doc}]}}
        resources[f"{camel(user)}Key"] = {"Type": "AWS::IAM::AccessKey", "Properties": {"UserName": {"Ref": uid}}}
        outputs[output_key(user, "AccessKeyId")] = {"Value": {"Ref": f"{camel(user)}Key"}}
        outputs[output_key(user, "SecretAccessKey")] = {"Value": {"Fn::GetAtt": [f"{camel(user)}Key", "SecretAccessKey"]}}
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"{env.aws_prefix}: S3 buckets and IAM users with static keys (aws-ec2)",
            "Resources": resources, "Outputs": outputs}


# ---- network ----
def network_template(env: Env) -> dict:
    """VPC、公開サブネット 3 つ（別 AZ。Fn::GetAZs の先頭 3 つ）、インターネットゲートウェイ、S3 のゲートウェイ
    エンドポイント（無料）、ノード用のセキュリティグループ 1 つ。NAT は置かない。"""
    plan = network_plan(env.ec2_vpc_cidr)
    res: dict = {
        "Vpc": {"Type": "AWS::EC2::VPC", "Properties": {
            "CidrBlock": plan["vpc"], "EnableDnsSupport": True, "EnableDnsHostnames": True,
            "Tags": [{"Key": "Name", "Value": env.ec2_prefix}]}},
        "Igw": {"Type": "AWS::EC2::InternetGateway", "Properties": {"Tags": [{"Key": "Name", "Value": env.ec2_prefix}]}},
        "IgwAttachment": {"Type": "AWS::EC2::VPCGatewayAttachment",
                          "Properties": {"VpcId": {"Ref": "Vpc"}, "InternetGatewayId": {"Ref": "Igw"}}},
        "RouteTable": {"Type": "AWS::EC2::RouteTable", "Properties": {"VpcId": {"Ref": "Vpc"}}},
        "DefaultRoute": {"Type": "AWS::EC2::Route", "DependsOn": "IgwAttachment", "Properties": {
            "RouteTableId": {"Ref": "RouteTable"}, "DestinationCidrBlock": "0.0.0.0/0", "GatewayId": {"Ref": "Igw"}}},
        "S3Endpoint": {"Type": "AWS::EC2::VPCEndpoint", "Properties": {
            "VpcId": {"Ref": "Vpc"}, "VpcEndpointType": "Gateway",
            "ServiceName": {"Fn::Sub": "com.amazonaws.${AWS::Region}.s3"}, "RouteTableIds": [{"Ref": "RouteTable"}]}},
        "NodeSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "GroupDescription": "litellm-lab aws-ec2 nodes: 80/443 from anywhere, everything between nodes",
            "VpcId": {"Ref": "Vpc"},
            "SecurityGroupIngress": [
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "CidrIp": "0.0.0.0/0", "Description": "HTTP"},
                {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0", "Description": "HTTPS"},
            ],
            "Tags": [{"Key": "Name", "Value": f"{env.ec2_prefix}-nodes"}]}},
        # k3s（6443、2379-2380、10250）、flannel（8472/udp）、VRRP（IP プロトコル 112）、NodePort をまとめて許す
        "NodeToNode": {"Type": "AWS::EC2::SecurityGroupIngress", "Properties": {
            "GroupId": {"Ref": "NodeSecurityGroup"}, "SourceSecurityGroupId": {"Ref": "NodeSecurityGroup"},
            "IpProtocol": "-1", "Description": "between nodes"}},
    }
    for i, sn in enumerate(plan["subnets"], start=1):
        res[f"Subnet{i}"] = {"Type": "AWS::EC2::Subnet", "Properties": {
            "VpcId": {"Ref": "Vpc"}, "CidrBlock": sn, "MapPublicIpOnLaunch": True,
            "AvailabilityZone": {"Fn::Select": [i - 1, {"Fn::GetAZs": ""}]},
            "Tags": [{"Key": "Name", "Value": f"{env.ec2_prefix}-{i}"}]}}
        res[f"Subnet{i}Route"] = {"Type": "AWS::EC2::SubnetRouteTableAssociation", "Properties": {
            "SubnetId": {"Ref": f"Subnet{i}"}, "RouteTableId": {"Ref": "RouteTable"}}}
    outputs = {"VpcId": {"Value": {"Ref": "Vpc"}}, "SecurityGroupId": {"Value": {"Ref": "NodeSecurityGroup"}}}
    for i in range(1, NODE_COUNT + 1):
        outputs[f"Subnet{i}Id"] = {"Value": {"Ref": f"Subnet{i}"}}
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"{env.aws_prefix}: VPC with 3 public subnets for the k3s nodes (aws-ec2)",
            "Resources": res, "Outputs": outputs}


# ---- nodes ----
def render_node_init(env: Env, node: dict, plan: dict, token_param: str, eip_allocation_id: str,
                     script: str | None = None) -> str:
    """node-init.sh の @@名前@@ を、この台の値で置き換える（Fn::Sub は使わない。シェルの ${...} と衝突するため）。"""
    text = script if script is not None else NODE_INIT_SCRIPT.read_text()
    peers = [n["private_ip"] for n in plan["nodes"] if n["index"] != node["index"]]
    values = {
        "REGION": env.aws_region, "NODE_INDEX": str(node["index"]), "SELF_IP": node["private_ip"],
        "FIRST_IP": plan["nodes"][0]["private_ip"], "PEER_IPS": " ".join(peers),
        "SECONDARY_IP": node["secondary_ip"], "K3S_VERSION": k3s_install_version(),
        "TOKEN_PARAM": token_param, "EIP_ALLOCATION_ID": eip_allocation_id,
        "PRIORITY": str(PRIORITIES[node["index"] - 1]), "REGISTRY_HOST": REGISTRY_HOST,
        "REGISTRY_NODEPORT": str(REGISTRY_NODEPORT),
    }
    for k, v in values.items():
        text = text.replace(f"@@{k}@@", v)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise ValueError(f"node-init.sh に置き換えていない名前が残っています: {left}")
    return text


def nodes_template(env: Env, token_param: str, eip_allocation_id: str, script: str | None = None) -> dict:
    """EC2 3 台（別サブネット、固定のプライベート IP 2 つずつ）、インスタンスロール、cloud-init。
    AMI は SSM の公開パラメータ（AL2023 arm64 の最新）。参加トークンは SSM のパラメータから取る。
    Elastic IP の割り当て ID は eip スタックの出力をそのまま埋め込む（keepalived のスクリプトが使う）。"""
    plan = network_plan(env.ec2_vpc_cidr)
    params: dict = {
        "AmiId": {"Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>", "Default": AMI_PARAMETER},
        "InstanceType": {"Type": "String", "Default": env.ec2_instance_type},
        "SecurityGroupId": {"Type": "AWS::EC2::SecurityGroup::Id"},
    }
    for i in range(1, NODE_COUNT + 1):
        params[f"Subnet{i}Id"] = {"Type": "AWS::EC2::Subnet::Id"}
    res: dict = {
        "NodeRole": {"Type": "AWS::IAM::Role", "Properties": {
            "RoleName": f"{env.ec2_prefix}-node",
            "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
            # Session Manager（構築ツールの接続とコマンドの実行）
            "ManagedPolicyArns": [{"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"}],
            "Policies": [{"PolicyName": "lab-node", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
                # Elastic IP の付け替え（keepalived の notify_master）
                {"Effect": "Allow", "Action": ["ec2:AssociateAddress", "ec2:DisassociateAddress", "ec2:DescribeAddresses"],
                 "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": env.aws_region}}},
                # k3s の参加トークン
                {"Effect": "Allow", "Action": ["ssm:GetParameter"],
                 "Resource": {"Fn::Sub": f"arn:${{AWS::Partition}}:ssm:${{AWS::Region}}:${{AWS::AccountId}}:parameter{token_param}"}},
            ]}}]}},
        "NodeProfile": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [{"Ref": "NodeRole"}]}},
    }
    for node in plan["nodes"]:
        i = node["index"]
        user_data = render_node_init(env, node, plan, token_param, eip_allocation_id, script)
        res[f"Node{i}"] = {"Type": "AWS::EC2::Instance", "Properties": {
            "ImageId": {"Ref": "AmiId"}, "InstanceType": {"Ref": "InstanceType"},
            "IamInstanceProfile": {"Ref": "NodeProfile"},
            "NetworkInterfaces": [{
                "DeviceIndex": "0", "SubnetId": {"Ref": f"Subnet{i}Id"}, "GroupSet": [{"Ref": "SecurityGroupId"}],
                "AssociatePublicIpAddress": True,
                # 2 つ目のプライベート IP に Elastic IP を付ける。1 つ目のパブリック IP は残るので、外向きの通信が途切れない
                "PrivateIpAddresses": [{"PrivateIpAddress": node["private_ip"], "Primary": True},
                                       {"PrivateIpAddress": node["secondary_ip"], "Primary": False}],
            }],
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {
                "VolumeType": "gp3", "VolumeSize": env.ec2_disk_gib, "Encrypted": True, "DeleteOnTermination": True}}],
            "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
            "UserData": {"Fn::Base64": user_data},
            "Tags": [{"Key": "Name", "Value": f"{env.ec2_prefix}-node{i}"}, {"Key": "lab:node-index", "Value": str(i)}],
        }}
    outputs = {
        "InstanceIds": {"Value": {"Fn::Join": [",", [{"Ref": f"Node{i}"} for i in range(1, NODE_COUNT + 1)]]}},
        "PrivateIps": {"Value": ",".join(n["private_ip"] for n in plan["nodes"])},
        "SecondaryIps": {"Value": ",".join(n["secondary_ip"] for n in plan["nodes"])},
    }
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"{env.aws_prefix}: 3 k3s server nodes with keepalived (aws-ec2)",
            "Parameters": params, "Resources": res, "Outputs": outputs}


def templates(env: Env, account: str, token_param: str, eip_allocation_id: str) -> dict[str, dict]:
    """4 スタックのテンプレート。nodes は eip スタックの出力（割り当て ID）が要るので、eip の配備の後に描く。"""
    return {"eip": eip_template(env), "storage": storage_template(env, account),
            "network": network_template(env), "nodes": nodes_template(env, token_param, eip_allocation_id)}


def write_template(doc: dict, path: Path) -> Path:
    import json
    from .core import write_if_changed
    write_if_changed(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path
