"""AWS 環境（LAB_TARGET=aws）だけで使う部品: 資格情報、CDK の配備と出力、kubeconfig、ECR、Secrets Manager、
フェイルオーバーの起こし方（変更仕様書 2026-10-24-AWS構成 2.3 節、変更設計書 3〜5 章）。

- AWS の基盤は cdk/（Python の CDK アプリ）が CloudFormation のスタックとして作る。ここはそれを呼び、
  出力（エンドポイント、バケット名、ロールの ARN など）を読んでほかの段階に渡す
- AWS の API は boto3（ツール自身の依存）で呼ぶ。AWS CLI は kubeconfig の発行（kubectl が aws eks get-token を
  呼ぶため）にだけ要る
- 資格情報は .env の LAB_AWS_PROFILE（AWS CLI のプロファイル。aws configure sso で作る）。空なら既定の探し方
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import render
from .core import (
    BUILD_DIR, ROOT, AWS_KUBE_CONTEXT, Env, LabError, info, kc, ok, run, warn, write_if_changed,
)

CDK_DIR = ROOT / "cdk"
OUTPUTS_FILE = BUILD_DIR / "aws-outputs.json"
SPEC_FILE = BUILD_DIR / "aws-spec.json"
# スタックの名前（接頭辞の後ろ）。計算資源（compute）だけを消しても、データ（S3、Aurora、秘密情報）は残る
STACKS = ["cert", "network", "data", "compute"]
# EKS Pod Identity で IAM ロールを結び付ける ServiceAccount（名前空間, 名前）。CDK と labtool が同じ定義を使う
POD_IDENTITIES = {
    "litellm-writer": ("litellm-lab", "litellm"),
    "audit-compactor": ("litellm-lab", "audit-compactor"),
    "audit-viewer": ("litellm-lab", "audit-viewer"),
    "external-secrets": ("external-secrets", "external-secrets"),
    "efs-csi": ("kube-system", "efs-csi-controller-sa"),
}
APP_SECRET_KEYS = [
    "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "AUDIT_SESSION_SECRET",
    "LAB_KEY_USER1_CLAUDE", "LAB_KEY_USER1_CODEX", "LAB_KEY_USER2_CLAUDE", "LAB_KEY_USER2_CODEX",
    "LAB_KEY_USER1_CLAUDE_NOREVIEW", "LAB_KEY_USER1_CODEX_NOREVIEW",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "TYPESAFE_API_KEY",
]


# ---- 資格情報と boto3 ----
def shell_env(env: Env) -> dict[str, str]:
    """aws / cdk / kubectl（aws eks get-token）に渡す環境変数。"""
    e = dict(os.environ)
    e["AWS_REGION"] = env.aws_region
    e["AWS_DEFAULT_REGION"] = env.aws_region
    if env.aws_profile:
        e["AWS_PROFILE"] = env.aws_profile
    return e


def session(env: Env):
    import boto3
    return boto3.Session(profile_name=env.aws_profile or None, region_name=env.aws_region)


def client(env: Env, service: str):
    return session(env).client(service)


def identity(env: Env) -> dict:
    """今の資格情報（アカウント、ARN）。無ければ、作り方を添えて止める。"""
    try:
        return client(env, "sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001 botocore の例外は種類が多い
        raise LabError(
            "AWS の資格情報が使えません（LAB_AWS_PROFILE=" + (env.aws_profile or "（既定）") + f"）: {e}\n"
            "  aws configure sso でプロファイルを作り、aws sso login --profile <名前> でサインインしてから、\n"
            "  .env の LAB_AWS_PROFILE にその名前を書いてください") from None


def operator_principal_arn(ident: dict, role_arn_of=None) -> str:
    """EKS のアクセスエントリに登録する IAM の主体。IAM Identity Center（SSO）の一時的な資格情報は
    assumed-role の ARN なので、IAM に問い合わせてロールの ARN に直す（SSO のロールは
    /aws-reserved/sso.amazonaws.com/<リージョン>/ のパスを持ち、パス無しの ARN は EKS が invalid principal と返す）。
    role_arn_of はロール名 → ARN（試験で差し替える）。"""
    arn = ident["Arn"]
    if ":assumed-role/" in arn:
        role = arn.split(":assumed-role/")[1].split("/")[0]
        return role_arn_of(role) if role_arn_of else f"arn:aws:iam::{ident['Account']}:role/{role}"
    return arn


def iam_role_arn(env: Env, role_name: str) -> str:
    return client(env, "iam").get_role(RoleName=role_name)["Role"]["Arn"]


# ---- CDK ----
def _cdk(env: Env, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """CDK の CLI を流す。どのコマンドも cdk.json のアプリを起動するので、定義（-c spec=）は常に渡す（bootstrap も）。"""
    cmd = ["npx", "--yes", "aws-cdk@2", *args, "-c", f"spec={SPEC_FILE}"]
    return run(cmd, capture=capture, cwd=CDK_DIR, env_vars=shell_env(env))


def availability_zones(env: Env) -> list[str]:
    """VPC に使う 3 つの AZ。LAB_AWS_AZS（末尾を , 区切り）があればそれ、無ければアカウントで使える AZ の先頭 3 つ
    （使える AZ はアカウントごとに違う。例: 東京は a,c,d のアカウントと b,c,d のアカウントがある）。"""
    suffixes = env.get("LAB_AWS_AZS", "").strip()
    if suffixes:
        return [env.aws_region + z.strip() for z in suffixes.split(",") if z.strip()]
    zones = client(env, "ec2").describe_availability_zones(
        Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}, {"Name": "state", "Values": ["available"]}]
    )["AvailabilityZones"]
    names = sorted(z["ZoneName"] for z in zones)
    if len(names) < 3:
        raise LabError(f"{env.aws_region} で使える AZ が 3 つ未満です: {names}")
    return names[:3]


def spec(env: Env, ident: dict) -> dict:
    """CDK に渡す定義（1 つの定義から labtool と CDK の両方に渡す。変更仕様書 2.3 節）。"""
    buckets = render.s3_buckets(env)
    return {
        "prefix": env.aws_prefix,
        "account": ident["Account"],
        "region": env.aws_region,
        "availability_zones": availability_zones(env),
        "hostname": env.aws_hostname,
        "operator_principal_arn": operator_principal_arn(ident, lambda r: iam_role_arn(env, r)),
        "cluster_name": env.aws_cluster_name,
        "kubernetes_version": env.get("LAB_EKS_VERSION", "1.34"),
        "bucket_prefix": render.bucket_prefix(env, ident["Account"]),
        "buckets": {name: {"lock": b["lock"], "versioning": b["versioning"], "lifecycle": b["lifecycle"]}
                    for name, b in buckets.items() if name != render.PG_BACKUP},
        "policies": {user: doc for user, doc in render.s3_policies().items() if user != "pg-backup"},
        "pod_identities": {k: {"namespace": ns, "service_account": sa} for k, (ns, sa) in POD_IDENTITIES.items()},
        "scale": env.get("LAB_AWS_SCALE", "lab"),
        "aurora_max_acu": float(env.get("LAB_AWS_AURORA_MAX_ACU", "4")),
        "aurora_pause_minutes": int(env.get("LAB_AWS_AURORA_PAUSE_MINUTES", "20")),
        "valkey_node_type": env.get("LAB_AWS_VALKEY_NODE_TYPE", "cache.t4g.micro"),
        "alb_scheme": env.get("LAB_AWS_ALB_SCHEME", "internet-facing"),
        "nat_gateways": int(env.get("LAB_AWS_NAT_GATEWAYS", "1")),
    }


def deploy(env: Env, ident: dict, stacks: list[str] | None = None) -> dict:
    """CDK の bootstrap と配備（差分が無ければ何もしない）。終わったら出力を返す。"""
    spec_text = json.dumps(spec(env, ident), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_if_changed(SPEC_FILE, spec_text)
    names = [f"{env.aws_prefix}-{s}" for s in (stacks or STACKS)]
    info(f"CDK を配備します: {', '.join(names)}（{env.aws_region}、アカウント {ident['Account']}）")
    _cdk(env, "bootstrap", f"aws://{ident['Account']}/{env.aws_region}")
    if "cert" in (stacks or STACKS):
        _cert_hint(env)
    _cdk(env, "deploy", *names, "--require-approval", "never", "--concurrency", "2",
         "--outputs-file", str(OUTPUTS_FILE))
    return outputs(env)


def destroy(env: Env, stacks: list[str]) -> None:
    ident = identity(env)
    write_if_changed(SPEC_FILE, json.dumps(spec(env, ident), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    names = [f"{env.aws_prefix}-{s}" for s in stacks]
    _cdk(env, "destroy", *names, "--force")


def _cert_hint(env: Env) -> None:
    """ACM の証明書は DNS 検証。手持ちの DNS に CNAME を足すまで CloudFormation が待つので、先に知らせる。"""
    acm = client(env, "acm")
    for c in acm.list_certificates(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]).get("CertificateSummaryList", []):
        if c.get("DomainName") == env.aws_hostname and c.get("Status") == "ISSUED":
            return
    warn(f"ACM の証明書（{env.aws_hostname}）はまだ発行されていません。配備の途中で CloudFormation が\n"
         "  「Content of DNS Record is: {Name: _xxx..., Type: CNAME, Value: _yyy.acm-validations.aws.}」と出したら、\n"
         "  その CNAME を手持ちの DNS に足してください（発行されるまで配備は待ちます）")


def outputs(env: Env) -> dict:
    """CloudFormation の出力をスタック横断で 1 つの辞書にする（.build/aws-outputs.json にも残す）。"""
    cf = client(env, "cloudformation")
    merged: dict[str, str] = {}
    for s in STACKS:
        name = f"{env.aws_prefix}-{s}"
        try:
            res = cf.describe_stacks(StackName=name)
        except Exception:  # noqa: BLE001 無いスタックは飛ばす（compute を消した後など）
            continue
        for o in res["Stacks"][0].get("Outputs", []) or []:
            merged[o["OutputKey"]] = o["OutputValue"]
    if not merged:
        raise LabError(f"CloudFormation の出力がありません（スタック {env.aws_prefix}-*）。先に ./lab cluster を流してください")
    write_if_changed(OUTPUTS_FILE, json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return merged


def load_outputs(env: Env) -> dict:
    if OUTPUTS_FILE.exists():
        return json.loads(OUTPUTS_FILE.read_text())
    return outputs(env)


# ---- kubeconfig ----
def kubeconfig(env: Env) -> None:
    """kubectl の context を EKS に向ける（aws eks update-kubeconfig。何度流しても同じ）。"""
    run(["aws", "eks", "update-kubeconfig", "--name", env.aws_cluster_name, "--alias", AWS_KUBE_CONTEXT,
         "--region", env.aws_region], env_vars=shell_env(env), quiet=True)
    ok(f"kubectl の context {AWS_KUBE_CONTEXT} を更新しました")


# ---- ECR ----
def ecr_login(env: Env, registry: str) -> None:
    tok = client(env, "ecr").get_authorization_token()["authorizationData"][0]
    import base64
    user, password = base64.b64decode(tok["authorizationToken"]).decode().split(":", 1)
    run(["docker", "login", "--username", user, "--password-stdin", registry], input=password, quiet=True)


def ecr_has_tag(env: Env, repository: str, tag: str) -> bool:
    try:
        res = client(env, "ecr").describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])
        return bool(res.get("imageDetails"))
    except Exception:  # noqa: BLE001 ImageNotFoundException など
        return False


# ---- Secrets Manager ----
def app_secret_name(env: Env) -> str:
    return f"{env.aws_prefix}/app"


def read_json_secret(env: Env, name: str) -> dict | None:
    sm = client(env, "secretsmanager")
    try:
        return json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        return None


def write_json_secret(env: Env, name: str, data: dict, description: str) -> None:
    sm = client(env, "secretsmanager")
    text = json.dumps(data, sort_keys=True)
    try:
        sm.create_secret(Name=name, SecretString=text, Description=description)
        info(f"Secrets Manager に {name} を作りました")
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=name, SecretString=text)


def ensure_app_secret(env: Env) -> dict:
    """アプリの秘密情報（{prefix}/app）。空の項目は生成し、.env にある値（Google の OAuth など）は写す。
    既にある値は上書きしない（Mac の .env と同じ決まり）。値はこの Env にも載せる（.env には書かない）。"""
    from .core import gen_key, gen_secret
    name = app_secret_name(env)
    data = read_json_secret(env, name) or {}
    before = dict(data)
    for key, make in (("LITELLM_MASTER_KEY", gen_key), ("LITELLM_SALT_KEY", gen_key),
                      ("AUDIT_SESSION_SECRET", lambda: gen_secret(32))):
        data.setdefault(key, make())
    for key in ("LAB_KEY_USER1_CLAUDE", "LAB_KEY_USER1_CODEX", "LAB_KEY_USER2_CLAUDE", "LAB_KEY_USER2_CODEX"):
        data.setdefault(key, gen_key())
    if render.noreview_enabled(env):
        for key in ("LAB_KEY_USER1_CLAUDE_NOREVIEW", "LAB_KEY_USER1_CODEX_NOREVIEW"):
            data.setdefault(key, gen_key())
    for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "TYPESAFE_API_KEY",
                "LAB_AUDIT_OIDC_CLIENT_ID", "LAB_AUDIT_OIDC_CLIENT_SECRET"):
        if env.get(key):
            data[key] = env.get(key)
    if data != before:
        write_json_secret(env, name, data, "litellm-agent-gateway-lab のアプリの秘密情報（./lab secrets が管理）")
        ok(f"Secrets Manager の {name} を更新しました（{len(data)} 項目）")
    else:
        ok(f"Secrets Manager の {name} は揃っています（{len(data)} 項目）")
    for k, v in data.items():
        env.values[k] = v
    return data


def load_app_secret(env: Env) -> None:
    """アプリの秘密情報を Env に載せる（bootstrap や試験が、マスターキーと利用者のキーを使うため）。"""
    data = read_json_secret(env, app_secret_name(env))
    if not data:
        raise LabError(f"Secrets Manager に {app_secret_name(env)} がありません。先に ./lab secrets を流してください")
    for k, v in data.items():
        env.values[k] = v


def database_resource_id(env: Env, out: dict) -> str:
    """Aurora クラスターの不変の ID（DB 移行の Job の名前に使う。作り直すと変わるので移行が流れ直る）。"""
    return out["AuroraClusterResourceId"]


# ---- ChatGPT のトークンを共有ボリュームへ ----
def sync_chatgpt_token(env: Env) -> None:
    """Mac の auth.json を、全 Pod で共有する EFS の PVC（chatgpt-token）に写す。使い捨ての Pod でコピーする。"""
    from .core import NS, kn, wait_until
    token = env.chatgpt_token_file
    pod = "chatgpt-token-sync"
    run(kn("delete", "pod", pod, "--ignore-not-found", "--wait=true"), quiet=True)
    manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": NS},
        "spec": {"restartPolicy": "Never",
                 "containers": [{"name": "sync", "image": "public.ecr.aws/docker/library/busybox:1.37",
                                 "command": ["sleep", "600"],
                                 "volumeMounts": [{"name": "token", "mountPath": "/token"}]}],
                 "volumes": [{"name": "token", "persistentVolumeClaim": {"claimName": "chatgpt-token"}}]},
    }
    run(kn("apply", "-f", "-"), input=json.dumps(manifest), quiet=True)
    try:
        wait_until(300, "トークンをコピーする Pod の起動", kn("wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=5s"))
        run(kn("cp", str(token), f"{pod}:/token/auth.json"), quiet=True)
        run(kn("exec", pod, "--", "chmod", "600", "/token/auth.json"), quiet=True)
        ok("ChatGPT のトークンを共有ボリュームに写しました")
    finally:
        run(kn("delete", "pod", pod, "--ignore-not-found", "--wait=false"), quiet=True)


# ---- フェイルオーバー（障害の試験。変更仕様書 7.2 節） ----
def aurora_failover(env: Env, out: dict) -> str:
    rds = client(env, "rds")
    cluster = out["AuroraClusterIdentifier"]
    before = _aurora_writer(rds, cluster)
    rds.failover_db_cluster(DBClusterIdentifier=cluster)
    return f"Aurora {cluster} のフェイルオーバーを起こした（writer は {before}）"


def _aurora_writer(rds, cluster: str) -> str:
    members = rds.describe_db_clusters(DBClusterIdentifier=cluster)["DBClusters"][0]["DBClusterMembers"]
    return next(m["DBInstanceIdentifier"] for m in members if m["IsClusterWriter"])


def aurora_writer(env: Env, out: dict) -> str:
    return _aurora_writer(client(env, "rds"), out["AuroraClusterIdentifier"])


def valkey_failover(env: Env, out: dict) -> str:
    ec = client(env, "elasticache")
    group = out["ValkeyReplicationGroupId"]
    ec.test_failover(ReplicationGroupId=group, NodeGroupId="0001")
    return f"ElastiCache {group} のフェイルオーバーを起こした（test-failover）"


def valkey_primary(env: Env, out: dict) -> str:
    ec = client(env, "elasticache")
    g = ec.describe_replication_groups(ReplicationGroupId=out["ValkeyReplicationGroupId"])["ReplicationGroups"][0]
    for m in g["NodeGroups"][0]["NodeGroupMembers"]:
        if m.get("CurrentRole") == "primary":
            return m["CacheClusterId"]
    return "?"


def drain_litellm_node(env: Env) -> str:
    """LiteLLM の Pod が載っているノードを 1 台 drain する（退避の試験）。試験の後に uncordon する。"""
    from .core import NS, output
    pods = json.loads(output(kc("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=litellm", "-o", "json")))
    nodes = sorted({p["spec"].get("nodeName") for p in pods["items"] if p["spec"].get("nodeName")})
    if not nodes:
        raise LabError("LiteLLM の Pod が載っているノードが見つかりません")
    node = nodes[0]
    run(kc("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=180s"), quiet=True)
    return node


def uncordon(node: str) -> None:
    run(kc("uncordon", node), check=False, quiet=True)


def wait_for_cname(env: Env, alb_host: str) -> None:
    """手持ちの DNS の CNAME（ホスト名 → ALB）が引けるまで待つ。引けない間は手順を知らせる。"""
    import socket
    host = env.aws_hostname
    start = time.monotonic()
    told = False
    while True:
        try:
            socket.getaddrinfo(host, 443)
            ok(f"{host} が解決できました")
            return
        except socket.gaierror:
            pass
        if not told:
            warn(f"手持ちの DNS に CNAME を足してください: {host}  →  {alb_host}\n"
                 "  （反映されるまで待ちます。Ctrl-C で中断しても、もう一度 ./lab up を流せば続きから進みます）")
            told = True
        if time.monotonic() - start > 3600:
            raise LabError(f"{host} が 1 時間たっても解決できません（CNAME を確かめてください）")
        time.sleep(10)
        sys.stderr.write(".")
        sys.stderr.flush()
