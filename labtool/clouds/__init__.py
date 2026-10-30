"""クラウド固有層（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-30-VM環境の共通化とGCP追加/ 3 章）。

計算資源・ネットワーク・浮動 IP・バケットと静的キー・削除を、クラウドの API で直接行う層。
aws_ec2.py と gcp.py が同じ関数の組（FUNCTIONS）を持ち、構築ツールは for_env(env) で選んだモジュールだけを呼ぶ。
VM の中の手順、SSH、k3s の版上げ、障害試験（labtool/vm.py）はクラウドを知らない。
作ったものの識別子は毎回 API で探し直す（CloudFormation の出力に当たる保存場所は持たない）。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from types import ModuleType

from ..core import Env, LabError

NODE_COUNT = 3
# クラスタの中のイメージ置き場（registry。保存先はバケット）。ノードは自分の NodePort 経由で引く（vm/node-init.sh）
REGISTRY_HOST = "lab-registry:5000"
REGISTRY_NODEPORT = 30500
IMAGES_BUCKET = "images"
# k3s のトークンと版を置く小さなバケットと鍵（VM が起動時に自分の権限で読む。./lab upgrade-k8s が版を更新する）
CLUSTER_BUCKET = "cluster"
TOKEN_KEY = "k3s/token"
VERSION_KEY = "k3s/version"
# バケットの静的キーの利用者（Mac の RustFS と同じ 4 つ ＋ イメージ置き場）
REGISTRY_USER = "registry"
STATIC_KEY_USERS = ["litellm-writer", "audit-compactor", "audit-viewer", "pg-backup", REGISTRY_USER]
# keepalived の優先度（1 番が高い。健全性の検査に失敗すると 100 下がる）
PRIORITIES = [200, 150, 100]
# 操作者の資格情報でも消せない（保持が切れるまで残る）バケットを知らせるための名前
LOCKED_BUCKETS = ("audit-raw", "audit-access")

# 両方の実装が持つ公開関数（単体試験が名前と引き数の一致を確かめる。変更設計書 3.2 節）
FUNCTIONS = [
    "identity", "ensure_apis", "bucket_prefix", "s3_endpoint", "ensure_network", "allow_ssh_from", "allow_web_from", "ensure_entry_ip",
    "ensure_buckets", "check_buckets", "ensure_static_keys", "put_object", "get_object", "ensure_nodes",
    "recreate_node", "discover", "entry_ip_holder", "stop_node", "start_node", "node_init_hooks", "destroy",
    "empty_buckets", "snapshot",
]


@dataclass
class Node:
    """VM 1 台。id は AWS がインスタンス ID、GCP が <ゾーン>/<名前>。"""
    id: str
    index: int
    private_ip: str
    public_ip: str
    zone: str


@dataclass
class Infra:
    """API で探した基盤のまとめ（discover の戻り値）。"""
    nodes: list[Node] = field(default_factory=list)
    entry_ip: str = ""
    entry_ip_id: str = ""
    buckets: dict[str, str] = field(default_factory=dict)
    bucket_prefix: str = ""
    region: str = ""
    network_id: str = ""
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
    GCP（False）はリージョンのサブネット 1 つ（先頭の /24）に 3 台を置き、.10 / .11 / .12。"""
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
    return {"vpc": str(net), "subnets": subnets, "nodes": nodes}


def bucket_specs(env: Env) -> dict[str, dict]:
    """6 バケットの仕様（論理名 → render.s3_buckets の形）。Mac と同じ 4 つに、イメージ置き場と k3s のトークン・版を足す。"""
    from .. import render
    specs = dict(render.s3_buckets(env))
    specs[IMAGES_BUCKET] = {"lock": None, "versioning": False, "lifecycle": []}
    specs[CLUSTER_BUCKET] = {"lock": None, "versioning": True, "lifecycle": []}
    return specs


def static_key_policies(env: Env) -> dict[str, dict]:
    """利用者ごとの権限（論理名のバケット。S3 の IAM の形）。Mac の RustFS と同じ 4 利用者 ＋ registry。"""
    from .. import render
    policies = dict(render.s3_policies())
    policies[REGISTRY_USER] = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"],
         "Resource": [f"arn:aws:s3:::{IMAGES_BUCKET}", f"arn:aws:s3:::{IMAGES_BUCKET}/*"]}]}
    return policies
