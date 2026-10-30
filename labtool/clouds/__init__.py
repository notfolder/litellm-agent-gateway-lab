"""クラウド固有層（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章）。

provision（作る・消す。LAB_INFRA=managed だけ）と setup（.env の識別子の資源を読む・止める・起こす。両モード）の関数を、
aws_ec2.py と gcp.py が同じ組（FUNCTIONS）で持つ。構築ツールは for_env(env) で選んだモジュールだけを呼ぶ。
VM の中の手順、SSH、k3s の版上げ、障害試験（labtool/vm.py）はクラウドを知らない。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from types import ModuleType

from .. import render
from ..core import Env, LabError

NODE_COUNT = 3
# クラスタの中のイメージ置き場（registry。保存先はバケットの images/）。ノードは自分の NodePort 経由で引く（vm/node-init.sh）
REGISTRY_HOST = "lab-registry:5000"
REGISTRY_NODEPORT = 30500
# k3s のトークンと版を置く鍵（1 バケットの cluster/ ディレクトリ。操作者が読み書きし、VM には SSH で渡す）
TOKEN_KEY = "cluster/k3s/token"
VERSION_KEY = "cluster/k3s/version"
# バケットの静的キーの利用者（Mac の RustFS と同じ 4 つ ＋ イメージ置き場）と、操作者（GCP で必須）
REGISTRY_USER = "registry"
STATIC_KEY_USERS = ["litellm-writer", "audit-compactor", "audit-viewer", "pg-backup", REGISTRY_USER]
OPERATOR_USER = "operator"
# keepalived の優先度（1 番が高い。健全性の検査に失敗すると 100 下がる）
PRIORITIES = [200, 150, 100]
# 中継（出口プロキシ）の VM が聞くポートと、サブネットの中の位置（.20）
PROXY_PORT = 3128
PROXY_HOST_INDEX = 19

# 両方の実装が持つ公開関数（単体試験が名前と引き数の一致を確かめる）
FUNCTIONS = [
    # 共通
    "identity", "ensure_apis", "s3_endpoint", "describe_nodes", "describe_entry_ip", "entry_ip_holder",
    "stop_node", "start_node", "node_init_hooks", "check_bucket", "snapshot", "empty_bucket",
    # provision（managed だけ）
    "provision", "destroy", "allow_ssh_from", "allow_web_from", "allow_proxy_from", "recreate_node",
    # provided の全削除
    "destroy_provided",
]


@dataclass
class Node:
    """VM 1 台。id は AWS がインスタンス ID、GCP が <ゾーン>/<名前>。index は台の番号（1〜3。中継は 0）。"""
    id: str
    index: int
    private_ip: str
    public_ip: str
    zone: str


@dataclass
class Infra:
    """setup が使う基盤のまとめ（.env の識別子から describe_* で組む）。"""
    nodes: list[Node] = field(default_factory=list)
    entry_ip: str = ""
    entry_ip_id: str = ""
    bucket: str = ""
    region: str = ""
    proxy: Node | None = None
    extra: dict = field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise LabError(f"ノード {node_id} が基盤にありません")

    def node_ids(self) -> list[str]:
        return [n.id for n in sorted(self.nodes, key=lambda n: n.index)]


def for_env(env: Env) -> ModuleType:
    """LAB_TARGET に合うクラウド固有層。"""
    if env.is_aws_ec2:
        from . import aws_ec2
        return aws_ec2
    if env.is_gcp_vm:
        from . import gcp
        return gcp
    raise LabError(f"LAB_TARGET={env.target} はクラウド固有層を使いません")


def network_plan(cidr: str, subnets_per_node: bool) -> dict:
    """VM の CIDR（/16）から各台の固定 IP を決める。
    AWS（subnets_per_node=True）は先頭の /24 × 3 を別 AZ の公開サブネットにし、各台は自分のサブネットの .10（と浮動 IP 用の .11）。
    GCP（False）はリージョンのサブネット 1 つ（先頭の /24）に 3 台を置き、.10 / .11 / .12。中継の VM は先頭のサブネットの .20。"""
    net = ipaddress.ip_network(cidr)
    blocks = list(net.subnets(new_prefix=24))
    nodes = []
    if subnets_per_node:
        subnets = [str(b) for b in blocks[:NODE_COUNT]]
        for i, sn in enumerate(blocks[:NODE_COUNT], start=1):
            hosts = list(sn.hosts())
            nodes.append({"index": i, "subnet": str(sn), "private_ip": str(hosts[9]), "secondary_ip": str(hosts[10])})
    else:
        subnets = [str(blocks[0])]
        hosts = list(blocks[0].hosts())
        for i in range(1, NODE_COUNT + 1):
            nodes.append({"index": i, "subnet": subnets[0], "private_ip": str(hosts[8 + i]), "secondary_ip": ""})
    proxy_ip = str(list(blocks[0].hosts())[PROXY_HOST_INDEX])
    return {"vpc": str(net), "subnets": subnets, "nodes": nodes, "proxy_ip": proxy_ip}


def static_key_policies(env: Env) -> dict[str, dict]:
    """利用者ごとの権限（論理名。S3 の IAM の形）。Mac の RustFS と同じ 4 利用者 ＋ registry（images）。
    原本と閲覧記録に保持を付けて書く利用者には PutObjectRetention を足す（S3 は保持を付けて書くときに要る）。"""
    policies = dict(render.s3_policies())
    policies[REGISTRY_USER] = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::images", "arn:aws:s3:::images/*"]}]}
    policies["litellm-writer"]["Statement"].append(
        {"Effect": "Allow", "Action": ["s3:PutObjectRetention"], "Resource": [f"arn:aws:s3:::{render.AUDIT_RAW}/raw/*"]})
    policies["audit-viewer"]["Statement"].append(
        {"Effect": "Allow", "Action": ["s3:PutObjectRetention"], "Resource": [f"arn:aws:s3:::{render.AUDIT_ACCESS}/*"]})
    return policies


def _split_logical_arn(arn: str) -> tuple[str, str | None]:
    """arn:aws:s3:::<論理名>[/<鍵のパターン>] → (論理名, 鍵のパターン or None)。"""
    path = arn.removeprefix("arn:aws:s3:::")
    logical, _, obj = path.partition("/")
    return logical, (obj if _ else None)


def s3_user_policies(env: Env, bucket: str) -> dict[str, dict]:
    """IAM ユーザーごとの権限を 1 バケットのディレクトリに写す（変更設計書 4.3 節）。
    一覧（ListBucket、ListBucketMultipartUploads）はバケット全体。全員に原本と閲覧記録の版の削除・保持の回避、
    バケットの設定変更の明示的な拒否を付ける。"""
    out = {}
    deny = {"Effect": "Deny", "Action": ["s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectLegalHold"],
            "Resource": [f"arn:aws:s3:::{bucket}/{logical}/*" for logical in render.RETAINED_STORES]}
    deny_bucket = {"Effect": "Deny", "Action": ["s3:DeleteBucket", "s3:PutBucketObjectLockConfiguration",
                                                 "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning"],
                   "Resource": [f"arn:aws:s3:::{bucket}"]}
    for user, doc in static_key_policies(env).items():
        statements = []
        for st in doc["Statement"]:
            bucket_level = [a for a in st["Action"] if a in ("s3:ListBucket", "s3:*")]
            resources = []
            for arn in st["Resource"]:
                logical, obj = _split_logical_arn(arn)
                if obj is None:
                    continue
                resources.append(f"arn:aws:s3:::{bucket}/{logical}/{obj}")
            if resources:
                statements.append({"Effect": "Allow", "Action": [a for a in st["Action"] if a != "s3:ListBucket"],
                                   "Resource": resources})
            if bucket_level:
                statements.append({"Effect": "Allow", "Action": ["s3:ListBucket", "s3:ListBucketMultipartUploads",
                                                                 "s3:GetBucketLocation"],
                                   "Resource": [f"arn:aws:s3:::{bucket}"]})
        out[user] = {"Version": "2012-10-17", "Statement": statements + [deny, deny_bucket]}
    return out


# GCS のロール。custom: は gcp.py がプロジェクトのカスタムロールの名前に写す
GCS_ROLE_WRITER = "custom:writer"     # objects.create ＋ objects.setRetention（追記と保持。読めない・消せない）
GCS_ROLE_LISTER = "custom:lister"     # objects.list ＋ buckets.get（一覧だけ）
GCS_ROLE_FOR_ACTION = {"s3:GetObject": "roles/storage.objectViewer", "s3:*": "roles/storage.objectAdmin"}


def gcs_bindings(env: Env, bucket: str) -> list[dict]:
    """利用者ごとの権限を GCS の条件付きバインディング（{user, role, prefix}。prefix は鍵の接頭辞。空なら条件なし）に写す。
    S3 の PutObject は、接頭辞限定（原本、閲覧記録）なら writer（追記 ＋ 保持）、ディレクトリ全体なら objectUser
    （上書きには delete が要る）。一覧は lister（条件なし。一覧だけ）。操作者は objectAdmin（条件なし）。"""
    out: list[dict] = []

    def add(user: str, role: str, prefix: str) -> None:
        entry = {"user": user, "role": role, "prefix": prefix}
        if entry not in out:
            out.append(entry)

    for user, doc in static_key_policies(env).items():
        for st in doc["Statement"]:
            for arn in st["Resource"]:
                logical, obj = _split_logical_arn(arn)
                for action in st["Action"]:
                    if action == "s3:ListBucket":
                        add(user, GCS_ROLE_LISTER, "")
                    elif action == "s3:PutObjectRetention":
                        continue   # writer のロールに含める
                    elif obj is None:
                        if action == "s3:*":
                            add(user, GCS_ROLE_LISTER, "")
                        continue
                    elif action == "s3:PutObject":
                        # 保持を付けるディレクトリ（原本、閲覧記録）は追記と保持だけの writer。それ以外は上書きがあるので objectUser
                        role = GCS_ROLE_WRITER if logical in render.RETAINED_STORES else "roles/storage.objectUser"
                        add(user, role, f"{logical}/{obj.rstrip('*')}")
                    else:
                        add(user, GCS_ROLE_FOR_ACTION[action], f"{logical}/{obj.rstrip('*')}")
    add(OPERATOR_USER, "roles/storage.objectAdmin", "")
    return out
