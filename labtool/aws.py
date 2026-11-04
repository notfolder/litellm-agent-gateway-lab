"""AWS 環境（LAB_TARGET=aws）だけで使う部品: 資格情報、CDK の配備と出力、kubeconfig、ECR、Secrets Manager、
フェイルオーバーの起こし方（変更仕様書 2026-10-24-AWS構成 2.3 節、変更設計書 3〜5 章）。

- AWS の基盤は cdk/（Python の CDK アプリ）が CloudFormation のスタックとして作る。ここはそれを呼び、
  出力（エンドポイント、バケット名、ロールの ARN など）を読んでほかの段階に渡す
- AWS の API は boto3（ツール自身の依存）で呼ぶ。AWS CLI は kubeconfig の発行（kubectl が aws eks get-token を
  呼ぶため）にだけ要る
- 資格情報は .env の LAB_AWS_PROFILE（AWS CLI のプロファイル。aws configure sso で作る）。空なら既定の探し方
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
        "kubernetes_version": env.get("LAB_EKS_VERSION", "1.35"),
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


# ---- ./lab down の後始末（スタックを消しても残るもの） ----
def empty_bucket(s3, bucket: str) -> int:
    """バケットの全オブジェクトの全版（削除マーカーも）を消す。GOVERNANCE の Object Lock は保持を越えて消す
    （操作者に s3:BypassGovernanceRetention が要る）。消した版の数を返す。aws-ec2 の削除（labtool/clouds/aws_ec2.py）も使う。"""
    n = 0
    # 保持を越える指定は Object Lock のあるバケットだけに付ける（無いバケットに付けると InvalidArgument。2026-11-04）
    try:
        s3.get_object_lock_configuration(Bucket=bucket)
        bypass = {"BypassGovernanceRetention": True}
    except s3.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ObjectLockConfigurationNotFoundError":
            raise
        bypass = {}
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                for k in ("Versions", "DeleteMarkers") for o in page.get(k, [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True}, **bypass)
            n += len(objs[i:i + 1000])
    return n


def retained_bucket_names(env: Env, account: str) -> list[str]:
    """data スタックを消しても残る S3 のバケット（RETAIN。Object Lock のため CloudFormation が空にできない）。"""
    prefix = render.bucket_prefix(env, account)
    return [prefix + logical for logical in render.s3_buckets(env) if logical != render.PG_BACKUP]


def purge_retained(env: Env) -> None:
    """スタックを消した後に残る有料・残留のものを消す: S3（GOVERNANCE のロックを越えて空にしてから）、
    アプリの秘密情報 {prefix}/app（復旧期間なし）、Aurora のスナップショット、ECR、EFS（RETAIN の版で作った残り）。
    Object Lock が COMPLIANCE のバケットは誰にも消せないので残して知らせる。"""
    ident = identity(env)
    s3 = client(env, "s3")
    lock_mode = env.get("LAB_AUDIT_LOCK_MODE", "GOVERNANCE")
    for bucket in retained_bucket_names(env, ident["Account"]):
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            continue
        if lock_mode == "COMPLIANCE" and bucket.endswith((render.AUDIT_RAW, render.AUDIT_ACCESS)):
            warn(f"バケット {bucket} は Object Lock（COMPLIANCE）のため消せません。保持期間が切れるまで残ります")
            continue
        n = empty_bucket(s3, bucket)
        s3.delete_bucket(Bucket=bucket)
        info(f"バケット {bucket} を消しました（{n} 版）")
    sm = client(env, "secretsmanager")
    try:
        sm.delete_secret(SecretId=app_secret_name(env), ForceDeleteWithoutRecovery=True)
        info(f"秘密情報 {app_secret_name(env)} を消しました")
    except sm.exceptions.ResourceNotFoundException:
        pass
    rds = client(env, "rds")
    for snap in rds.describe_db_cluster_snapshots(SnapshotType="manual")["DBClusterSnapshots"]:
        if snap["DBClusterIdentifier"].startswith(env.aws_prefix):
            rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=snap["DBClusterSnapshotIdentifier"])
            info(f"Aurora のスナップショット {snap['DBClusterSnapshotIdentifier']} を消しました")
    # ElastiCache も、data スタックを消すときに CloudFormation が最後のスナップショットを取る（2026-11-04 に残っていた）
    ec = client(env, "elasticache")
    for snap in ec.describe_snapshots()["Snapshots"]:
        name = snap["SnapshotName"]
        owners = (name, snap.get("ReplicationGroupId") or "", snap.get("CacheClusterId") or "")
        if not any(o.startswith(env.aws_prefix) for o in owners):   # 自動の分は名前が automatic. で始まる
            continue
        try:
            ec.delete_snapshot(SnapshotName=name)
            info(f"ElastiCache のスナップショット {name} を消しました")
        except ec.exceptions.ClientError as e:
            warn(f"ElastiCache のスナップショット {name} を消せませんでした（{e.response['Error']['Code']}）")
    ecr = client(env, "ecr")
    for repo in ("litellm-lab", "audit-lab"):
        try:
            ecr.delete_repository(repositoryName=f"{env.aws_prefix}/{repo}", force=True)
            info(f"ECR {env.aws_prefix}/{repo} を消しました")
        except ecr.exceptions.RepositoryNotFoundException:
            pass
    efs = client(env, "efs")
    for fs in efs.describe_file_systems()["FileSystems"]:
        tags = {t["Key"]: t["Value"] for t in fs.get("Tags", [])}
        if tags.get("aws:cloudformation:stack-name") != f"{env.aws_prefix}-data":
            continue
        for mt in efs.describe_mount_targets(FileSystemId=fs["FileSystemId"])["MountTargets"]:
            efs.delete_mount_target(MountTargetId=mt["MountTargetId"])
        for _ in range(60):
            if not efs.describe_mount_targets(FileSystemId=fs["FileSystemId"])["MountTargets"]:
                break
            time.sleep(5)
        efs.delete_file_system(FileSystemId=fs["FileSystemId"])
        info(f"EFS {fs['FileSystemId']} を消しました")
    # CloudWatch Logs のロググループ（EKS の制御部分、CDK のカスタムリソースの Lambda）はスタックを消しても残る
    logs = client(env, "logs")
    for prefix in (f"/aws/eks/{env.aws_cluster_name}/", f"/aws/lambda/{env.aws_prefix}-"):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for g in page["logGroups"]:
                logs.delete_log_group(logGroupName=g["logGroupName"])
                info(f"ロググループ {g['logGroupName']} を消しました")


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


def load_app_secret(env: Env, required: bool = True) -> None:
    """アプリの秘密情報を Env に載せる（bootstrap や試験が、マスターキーと利用者のキーを使うため）。
    .env に同じ名前の値（Mac の環境のキーなど）があっても、Secrets Manager の値で上書きする。
    required=False なら、まだ作っていないとき（初回の ./lab up の secrets より前）は何もしない。"""
    data = read_json_secret(env, app_secret_name(env))
    if not data and not required:
        return
    if not data:
        raise LabError(f"Secrets Manager に {app_secret_name(env)} がありません。先に ./lab secrets を流してください")
    for k, v in data.items():
        env.values[k] = v


def database_resource_id(env: Env, out: dict) -> str:
    """Aurora クラスターの不変の ID（DB 移行の Job の名前に使う。作り直すと変わるので移行が流れ直る）。"""
    return out["AuroraClusterResourceId"]


# ---- ChatGPT のトークンを共有ボリュームへ ----
TOKEN_POD = "chatgpt-token-sync"
TOKEN_MARK = "/token/.synced-from-mac"     # 最後に Mac から写した auth.json のハッシュ（ボリュームの中に残す）
TOKEN_ANNOTATION = "litellm-lab/token-sha256"


@contextlib.contextmanager
def _token_pod(env: Env):
    """共有ボリューム（PVC chatgpt-token）を /token に付けた使い捨ての Pod。終わったら消す。"""
    from .core import NS, kn, wait_until
    run(kn("delete", "pod", TOKEN_POD, "--ignore-not-found", "--wait=true"), quiet=True)
    manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": TOKEN_POD, "namespace": NS},
        "spec": {"restartPolicy": "Never",
                 "containers": [{"name": "sync", "image": "public.ecr.aws/docker/library/busybox:1.37",
                                 "command": ["sleep", "600"],
                                 "volumeMounts": [{"name": "token", "mountPath": "/token"}]}],
                 "volumes": [{"name": "token", "persistentVolumeClaim": {"claimName": "chatgpt-token"}}]},
    }
    run(kn("apply", "-f", "-"), input=json.dumps(manifest), quiet=True)
    try:
        wait_until(300, "トークンを扱う Pod の起動", kn("wait", "--for=condition=Ready", f"pod/{TOKEN_POD}", "--timeout=5s"))
        yield TOKEN_POD
    finally:
        run(kn("delete", "pod", TOKEN_POD, "--ignore-not-found", "--wait=false", "--grace-period=1"), quiet=True)


def sync_chatgpt_token(env: Env) -> None:
    """Mac の auth.json を、全 Pod で共有する EFS の PVC（chatgpt-token）に写す。
    LiteLLM はトークンを更新すると、使い捨てのリフレッシュトークンをボリュームにだけ書く。だから写すのは
    「ボリュームにトークンが無い」か「Mac の auth.json が、最後に写したときから変わった（サインインし直した）」ときだけにする。
    それ以外はボリュームのトークン（更新済みかもしれない）を使い続ける。写した中身のハッシュはボリュームの中（TOKEN_MARK）と
    PVC の注釈に残し、注釈が同じなら Pod も作らない（./lab up を何度流しても何も変えない）。"""
    import hashlib
    from .core import kn, output
    token = env.chatgpt_token_file
    digest = hashlib.sha256(token.read_bytes()).hexdigest()[:32]
    noted = output(kn("get", "pvc", "chatgpt-token", "-o",
                      "jsonpath={.metadata.annotations.litellm-lab/token-sha256}"), check=False).strip()
    if noted == digest:
        ok("ChatGPT のトークンは共有ボリュームにあります（Mac の auth.json は前に写したときから変わっていません）")
        return
    with _token_pod(env) as pod:
        state = output(kn("exec", pod, "--", "sh", "-c",
                          f"test -s /token/auth.json && echo has || echo none; cat {TOKEN_MARK} 2>/dev/null"),
                       check=False).split()
        has_token, marked = (state[0] == "has") if state else False, (state[1] if len(state) > 1 else "")
        if has_token and marked == digest:
            ok("ChatGPT のトークンは共有ボリュームのものを使います（Mac の auth.json は前に写したときから変わっていません）")
        else:
            run(kn("cp", str(token), f"{pod}:/token/auth.json"), quiet=True)
            run(kn("exec", pod, "--", "sh", "-c", f"chmod 600 /token/auth.json && echo {digest} > {TOKEN_MARK}"), quiet=True)
            ok("ChatGPT のトークンを共有ボリュームに写しました"
               + ("（Mac でサインインし直したため）" if has_token else "（ボリュームが空だったため）"))
        run(kn("annotate", "pvc", "chatgpt-token", f"{TOKEN_ANNOTATION}={digest}", "--overwrite"), quiet=True)


def pull_chatgpt_token(env: Env) -> None:
    """共有ボリュームのトークン（LiteLLM が更新したもの）を Mac の auth.json に戻す。./lab down が EFS を消す前に呼ぶ。
    戻さないと、Mac に残るのは使えなくなったリフレッシュトークンで、次の ./lab up で LiteLLM が起動しない（2026-11-03）。"""
    import tempfile
    from .core import kn
    token = env.chatgpt_token_file
    if not succeeds_quiet(kn("get", "pvc", "chatgpt-token")):
        return
    with _token_pod(env) as pod, tempfile.TemporaryDirectory() as tmp:
        got = Path(tmp) / "auth.json"
        if run(kn("cp", f"{pod}:/token/auth.json", str(got)), check=False, quiet=True).returncode != 0 or not got.exists():
            warn("共有ボリュームに ChatGPT のトークンがありません（Mac の auth.json はそのまま）")
            return
        try:
            data = json.loads(got.read_text())
        except ValueError:
            warn("共有ボリュームの ChatGPT のトークンが読めません（Mac の auth.json はそのまま）")
            return
        if not data.get("refresh_token"):
            warn("共有ボリュームの ChatGPT のトークンにリフレッシュトークンがありません（Mac の auth.json はそのまま）")
            return
        if token.exists() and token.read_bytes() == got.read_bytes():
            ok("ChatGPT のトークンは Mac と共有ボリュームで同じです")
            return
        token.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = token.with_suffix(".json.tmp")
        tmp_dest.write_bytes(got.read_bytes())
        tmp_dest.chmod(0o600)
        tmp_dest.replace(token)
        ok(f"共有ボリュームの ChatGPT のトークン（LiteLLM が更新したもの）を Mac に戻しました（{token}）")


def succeeds_quiet(cmd: list[str]) -> bool:
    return run(cmd, check=False, quiet=True, timeout=60).returncode == 0


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
    # 移す先のノードが無ければ Auto Mode が足す（約 2 分）。PDB（maxUnavailable 1）で 1 つずつ移り、LiteLLM の起動も待つので
    # 180 秒では足りなかった（2026-11-03）。失敗したらノードを戻してから止める
    try:
        run(kc("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=600s"), quiet=True)
    except Exception:
        uncordon(node)
        raise
    return node


def uncordon(node: str) -> None:
    run(kc("uncordon", node), check=False, quiet=True)


def wait_for_cname(env: Env, alb_host: str) -> None:
    """手持ちの DNS の CNAME（ホスト名 → ALB）が引けるまで待つ。引けない間は手順を知らせる。

    名前が引けるだけでは足りない（前の構成の古いレコードが残っていることがある）。ホスト名の IP が
    ALB の IP と重なるまで待つ。ALB の名前は作成から数分は引けないので、その間も待つ。
    """
    import socket

    def addrs(name: str) -> set[str]:
        try:
            return {a[4][0] for a in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)}
        except socket.gaierror:
            return set()

    host = env.aws_hostname
    start = time.monotonic()
    told = False
    while True:
        got, alb = addrs(host), addrs(alb_host)
        if got and alb and got & alb:
            ok(f"{host} が ALB に解決できました")
            return
        if got and alb and not told:
            warn(f"{host} は ALB 以外（{', '.join(sorted(got))}）に解決されます。古いレコードを書き換えてください")
        if not told:
            warn(f"手持ちの DNS に CNAME を足してください: {host}  →  {alb_host}\n"
                 "  （反映されるまで待ちます。Ctrl-C で中断しても、もう一度 ./lab up を流せば続きから進みます）")
            told = True
        if time.monotonic() - start > 3600:
            raise LabError(f"{host} が 1 時間たっても解決できません（CNAME を確かめてください）")
        time.sleep(10)
        sys.stderr.write(".")
        sys.stderr.flush()
