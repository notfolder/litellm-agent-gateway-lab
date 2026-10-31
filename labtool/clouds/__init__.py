"""クラウド固有層（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章、
.history/2026-10-31-内部LBとVM側の防御とZabbix/ 3 章）。

provision（作る・消す。LAB_INFRA=managed だけ）と setup（.env の識別子の資源を読む・止める・起こす。両モード）の関数を、
aws_ec2.py と gcp.py が同じ組（FUNCTIONS）で持つ。構築ツールは for_env(env) で選んだモジュールだけを呼ぶ。
VM の中の手順、SSH、k3s の版上げ、障害試験（labtool/vm.py）はクラウドを知らない。
入口は内部ロードバランサ（AWS: 内部 NLB、GCP: 内部パススルー NLB）の固定の内部 IP。VM はクラウドの API を呼ばない
（keepalived と浮動 IP の付け替えは 2026-10-31 に廃止）。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from types import ModuleType

from .. import render
from ..core import Env, LabError

NODE_COUNT = 3          # 今の 3 台（k3s の server。etcd を持つ）。足す台（k3s の agent）は別に数える
WORKER_INDEX_BASE = 100  # 足す台の Node.index は 100 ＋ 番号（server の 1〜3、中継の 0 と重ならない）
WORKER_HOST_OFFSET = 30  # 足す台 i の内部 IP はサブネットの .30+i
# クラスタの中のイメージ置き場（registry。保存先はバケットの images/）。ノードは自分の NodePort 経由で引く（vm/node-init.sh）
REGISTRY_HOST = "lab-registry:5000"
REGISTRY_NODEPORT = 30500
# k3s のトークンと版を置く鍵（1 バケットの cluster/ ディレクトリ。操作者が読み書きし、VM には SSH で渡す）
TOKEN_KEY = "cluster/k3s/token"
VERSION_KEY = "cluster/k3s/version"
# バケットの静的キーの利用者（Mac の RustFS と同じ 4 つ ＋ イメージ置き場）と、操作者（GCP で必須）
REGISTRY_USER = "registry"
STATIC_KEY_USERS = ["litellm-writer", "audit-compactor", "audit-viewer", "pg-backup", REGISTRY_USER]   # 論理の利用者
OPERATOR_USER = "operator"
# 実際に作る静的キー（IAM ユーザー／サービスアカウント）は 2 つ（render.VM_KEY_ROLES）。論理の利用者の権限を役割ごとに合併する
KEY_ROLES = render.VM_KEY_ROLES
LEGACY_KEY_USERS = render.LEGACY_KEY_USERS
# 中継（出口プロキシ。検証では踏み台と Zabbix サーバーも兼ねる）の VM が聞くポートと、サブネットの中の位置（.20）
PROXY_PORT = 3128
PROXY_HOST_INDEX = 19
# 内部ロードバランサの固定の内部 IP の位置（各サブネットの .5。AWS は 3 サブネットに 1 つずつ、GCP は 1 つ）
LB_HOST_INDEX = 4
# Zabbix（agent 2 は 10050 で聞き、サーバーと proxy は 10051 で受ける）
ZABBIX_AGENT_PORT = 10050
ZABBIX_SERVER_PORT = 10051
# 内部ロードバランサのヘルスチェックの送信元（nftables とクラウドのファイアウォールで通す）。
# GCP は Google の範囲、AWS は VPC の中（NLB のアドレス）なので VPC の CIDR
GCP_HEALTH_CHECK_RANGES = ["35.191.0.0/16", "130.211.0.0/22"]
# VM が中継を通さず直接届く必要があるクラウドの内部アドレス（メタデータ、DNS、NTP）
GCP_META_IPS = ["169.254.169.254"]
AWS_META_IPS = ["169.254.169.254", "169.254.169.253", "169.254.169.123"]
# クラウドの DNS（cert-manager の DNS-01 の伝搬確認に使う再帰リゾルバ。Pod から外の権威サーバーへは届かないため）
GCP_DNS_IPS = ["169.254.169.254"]
AWS_DNS_IPS = ["169.254.169.253"]

# 両方の実装が持つ公開関数（単体試験が名前と引き数の一致を確かめる）
FUNCTIONS = [
    # 共通
    "identity", "ensure_apis", "s3_endpoint", "describe_nodes", "describe_entry_ip", "lb_targets_healthy",
    "health_check_ranges", "meta_ips", "dns_ips", "stop_node", "start_node", "check_bucket", "snapshot", "empty_bucket",
    # provision（managed だけ）
    "provision", "destroy", "allow_ssh_from", "allow_proxy_from", "recreate_node",
    # 足す台（managed だけ。変更設計書 2026-10-31-台の増減とLiteLLMのメモリ 2 章）
    "ensure_workers", "list_workers", "remove_workers",
    # provided の全削除
    "destroy_provided",
]


@dataclass
class Node:
    """VM 1 台。id は AWS がインスタンス ID、GCP が <ゾーン>/<名前>。index は台の番号（1〜3。中継は 0、足す台は 100 ＋ 番号）。
    role は server（今の 3 台）か worker（足す台）。"""
    id: str
    index: int
    private_ip: str
    public_ip: str
    zone: str
    role: str = "server"


@dataclass
class Infra:
    """setup が使う基盤のまとめ（.env の識別子から describe_* で組む）。
    entry_ip は内部ロードバランサの固定の内部 IP、entry_ip_id はその識別子（AWS: NLB の ARN、GCP: 転送ルールの名前）。
    bastion_host は踏み台の SSH の宛先、bastion_cidr は踏み台の内部 IP（管理系の経路と 22 の許可）。"""
    nodes: list[Node] = field(default_factory=list)
    workers: list[Node] = field(default_factory=list)
    entry_ip: str = ""
    entry_ip_id: str = ""
    bucket: str = ""
    region: str = ""
    proxy: Node | None = None
    bastion_host: str = ""
    bastion_cidr: str = ""
    extra: dict = field(default_factory=dict)

    def all_nodes(self) -> list[Node]:
        """今の 3 台と足す台（台の番号の順）。"""
        return sorted([*self.nodes, *self.workers], key=lambda n: n.index)

    def node(self, node_id: str) -> Node:
        for n in self.all_nodes():
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
    AWS（subnets_per_node=True）は先頭の /24 × 3 を別 AZ の公開サブネットにし、各台は自分のサブネットの .10。
    GCP（False）はリージョンのサブネット 1 つ（先頭の /24）に 3 台を置き、.10 / .11 / .12。中継（踏み台）の VM は先頭のサブネットの .20。
    内部ロードバランサの固定の内部 IP（lb_ips）は各サブネットの .5（AWS は 3 つ、GCP は 1 つ。入口は先頭のもの）。"""
    net = ipaddress.ip_network(cidr)
    blocks = list(net.subnets(new_prefix=24))
    nodes = []
    if subnets_per_node:
        subnets = [str(b) for b in blocks[:NODE_COUNT]]
        for i, sn in enumerate(blocks[:NODE_COUNT], start=1):
            hosts = list(sn.hosts())
            nodes.append({"index": i, "subnet": str(sn), "private_ip": str(hosts[9])})
    else:
        subnets = [str(blocks[0])]
        hosts = list(blocks[0].hosts())
        for i in range(1, NODE_COUNT + 1):
            nodes.append({"index": i, "subnet": subnets[0], "private_ip": str(hosts[8 + i])})
    proxy_ip = str(list(blocks[0].hosts())[PROXY_HOST_INDEX])
    lb_ips = [str(list(ipaddress.ip_network(s).hosts())[LB_HOST_INDEX]) for s in subnets]
    return {"vpc": str(net), "subnets": subnets, "nodes": nodes, "proxy_ip": proxy_ip, "lb_ips": lb_ips}


def worker_plan(cidr: str, subnets_per_node: bool, count: int) -> list[dict]:
    """足す台 1〜count の固定の内部 IP。GCP（subnets_per_node=False）はリージョンのサブネットの .30+i、AWS は 3 つのサブネットを
    順に使い、そのサブネットの .30+i（番号が違えば IP も違う）。"""
    blocks = list(ipaddress.ip_network(cidr).subnets(new_prefix=24))
    out = []
    for i in range(1, count + 1):
        sn = blocks[(i - 1) % NODE_COUNT] if subnets_per_node else blocks[0]
        hosts = list(sn.hosts())
        if WORKER_HOST_OFFSET + i - 1 >= len(hosts) - 1:
            raise LabError(f"足す台が多すぎます（{count} 台。サブネット {sn} に入りきらない）")
        out.append({"number": i, "index": WORKER_INDEX_BASE + i, "subnet": str(sn),
                    "subnet_index": (i - 1) % NODE_COUNT if subnets_per_node else 0,
                    "private_ip": str(hosts[WORKER_HOST_OFFSET + i - 1])})
    return out


def static_key_policies(env: Env) -> dict[str, dict]:
    """論理の利用者ごとの権限（論理名。S3 の IAM の形）。Mac の RustFS と同じ 4 利用者 ＋ registry（images）＋ 操作者
    （k3s のトークンと版の cluster/、片付けで消す audit-derived/、バケットの設定の読み取り）。
    原本と閲覧記録に保持を付けて書く利用者には PutObjectRetention を足す（S3 は保持を付けて書くときに要る）。"""
    policies = {u: {"Version": d["Version"], "Statement": [dict(s) for s in d["Statement"]]} for u, d in render.s3_policies().items()}
    policies[REGISTRY_USER] = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::images", "arn:aws:s3:::images/*"]}]}
    policies[OPERATOR_USER] = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:*"], "Resource": ["arn:aws:s3:::cluster", "arn:aws:s3:::cluster/*"]},
        {"Effect": "Allow", "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": [f"arn:aws:s3:::{render.AUDIT_DERIVED}/*"]},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketVersioning", "s3:GetBucketObjectLockConfiguration",
                                       "s3:GetBucketPublicAccessBlock", "s3:GetLifecycleConfiguration"],
         "Resource": [f"arn:aws:s3:::{render.AUDIT_RAW}", f"arn:aws:s3:::{render.AUDIT_ACCESS}", f"arn:aws:s3:::{render.AUDIT_DERIVED}"]},
    ]}
    policies["litellm-writer"]["Statement"].append(
        {"Effect": "Allow", "Action": ["s3:PutObjectRetention"], "Resource": [f"arn:aws:s3:::{render.AUDIT_RAW}/raw/*"]})
    policies["audit-viewer"]["Statement"].append(
        {"Effect": "Allow", "Action": ["s3:PutObjectRetention"], "Resource": [f"arn:aws:s3:::{render.AUDIT_ACCESS}/*"]})
    return policies


def role_policies(env: Env) -> dict[str, list[dict]]:
    """鍵の役割（2 つ）ごとに、論理の利用者の文を合併する（同じ文は 1 つに）。"""
    out: dict[str, list[dict]] = {role: [] for role in KEY_ROLES}
    for user, doc in static_key_policies(env).items():
        role = render.vm_key_role(user)
        for st in doc["Statement"]:
            if st not in out[role]:
                out[role].append(st)
    return out


def _split_logical_arn(arn: str) -> tuple[str, str | None]:
    """arn:aws:s3:::<論理名>[/<鍵のパターン>] → (論理名, 鍵のパターン or None)。"""
    path = arn.removeprefix("arn:aws:s3:::")
    logical, _, obj = path.partition("/")
    return logical, (obj if _ else None)


def s3_user_policies(env: Env, bucket: str) -> dict[str, dict]:
    """鍵の役割（IAM ユーザー 2 つ）ごとの権限を 1 バケットのディレクトリに写す（変更設計書 4.3 節、2026-10-31-静的キーを2つに）。
    一覧（ListBucket、ListBucketMultipartUploads）はバケット全体。両方に原本と閲覧記録の版の削除・保持の回避、
    バケットの設定変更の明示的な拒否を付ける。"""
    out = {}
    deny = {"Effect": "Deny", "Action": ["s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectLegalHold"],
            "Resource": [f"arn:aws:s3:::{bucket}/{logical}/*" for logical in render.RETAINED_STORES]}
    deny_bucket = {"Effect": "Deny", "Action": ["s3:DeleteBucket", "s3:PutBucketObjectLockConfiguration",
                                                 "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning"],
                   "Resource": [f"arn:aws:s3:::{bucket}"]}
    bucket_actions = {"s3:ListBucket", "s3:*", "s3:ListBucketVersions", "s3:GetBucketVersioning", "s3:GetBucketObjectLockConfiguration",
                      "s3:GetBucketPublicAccessBlock", "s3:GetLifecycleConfiguration"}
    for role, role_statements in role_policies(env).items():
        statements: list[dict] = []
        bucket_level: set[str] = set()
        for st in role_statements:
            bucket_level |= {a for a in st["Action"] if a in bucket_actions}
            resources = []
            for arn in st["Resource"]:
                logical, obj = _split_logical_arn(arn)
                if obj is None:
                    continue
                resources.append(f"arn:aws:s3:::{bucket}/{logical}/{obj}")
            actions = [a for a in st["Action"] if a not in bucket_actions or a == "s3:*"]
            if resources and actions:
                new = {"Effect": "Allow", "Action": actions, "Resource": resources}
                if new not in statements:
                    statements.append(new)
        if bucket_level:
            actions = sorted({"s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucketLocation"}
                             | (bucket_level - {"s3:*", "s3:ListBucket"}))
            statements.append({"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}"]})
        out[role] = {"Version": "2012-10-17", "Statement": statements + [deny, deny_bucket]}
    return out


# GCS のロール。custom: は gcp.py がプロジェクトのカスタムロールの名前に写す
GCS_ROLE_WRITER = "custom:writer"     # objects.create ＋ objects.setRetention（追記と保持。読めない・消せない）
GCS_ROLE_LISTER = "custom:lister"     # objects.list ＋ buckets.get（一覧だけ）
GCS_ROLE_FOR_ACTION = {"s3:GetObject": "roles/storage.objectViewer", "s3:*": "roles/storage.objectAdmin"}


def gcs_bindings(env: Env, bucket: str) -> list[dict]:
    """鍵の役割（サービスアカウント 2 つ）ごとの権限を GCS の条件付きバインディング（{user, role, prefix}。prefix は鍵の接頭辞。
    空なら条件なし）に写す。S3 の PutObject は、接頭辞限定（原本、閲覧記録）なら writer（追記 ＋ 保持）、ディレクトリ全体なら
    objectUser（上書きと削除ができる）。一覧とバケットの設定の読み取りは lister（条件なし）。s3:* は objectAdmin（接頭辞付き）。"""
    out: list[dict] = []

    def add(user: str, role: str, prefix: str) -> None:
        entry = {"user": user, "role": role, "prefix": prefix}
        if entry not in out:
            out.append(entry)

    bucket_reads = {"s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketVersioning", "s3:GetBucketObjectLockConfiguration",
                    "s3:GetBucketPublicAccessBlock", "s3:GetLifecycleConfiguration"}
    for key_role, statements in role_policies(env).items():
        for st in statements:
            for arn in st["Resource"]:
                logical, obj = _split_logical_arn(arn)
                for action in st["Action"]:
                    if action in bucket_reads:
                        add(key_role, GCS_ROLE_LISTER, "")
                    elif action == "s3:PutObjectRetention":
                        continue   # writer のロールに含める
                    elif obj is None:
                        if action == "s3:*":
                            add(key_role, GCS_ROLE_LISTER, "")
                        continue
                    elif action == "s3:PutObject":
                        # 保持を付けるディレクトリ（原本、閲覧記録）は追記と保持だけの writer。それ以外は上書きがあるので objectUser
                        role = GCS_ROLE_WRITER if logical in render.RETAINED_STORES else "roles/storage.objectUser"
                        add(key_role, role, f"{logical}/{obj.rstrip('*')}")
                    elif action in ("s3:DeleteObject", "s3:DeleteObjectVersion"):
                        add(key_role, "roles/storage.objectUser", f"{logical}/{obj.rstrip('*')}")
                    else:
                        add(key_role, GCS_ROLE_FOR_ACTION[action], f"{logical}/{obj.rstrip('*')}")
    return out
