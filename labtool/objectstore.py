"""オブジェクトストレージの設定: バケット、利用者、権限（変更設計書 2 章）。Mac は RustFS、VM の環境はクラウドのバケット。

S3 の API（boto3）と RustFS の管理 API（/rustfs/admin/v3。SigV4 で署名）を使う。./lab は Mac の上で動くので、
呼ぶ側が port_forward で張った接続先（http://127.0.0.1:<ポート>）を渡す。
どの操作も「無ければ作る、あれば比べて違えば直す」で、何度流しても同じ状態になる。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import contextlib

from . import render
from .core import Env, LabError, info, ok, port_forward

REGION = "us-east-1"
# boto3 1.36 以降の既定（送信時のチェックサム、aws-chunked）を GCS の XML API が SignatureDoesNotMatch で拒む。
# S3 互換のストレージ向けの公式の設定（AWS S3 と RustFS には害が無い。配備で判明 2026-10-30）
CHECKSUM_WHEN_REQUIRED = {"request_checksum_calculation": "when_required", "response_checksum_validation": "when_required"}


def s3_client(endpoint: str, access_key: str, secret_key: str):
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key, region_name=REGION,
                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}, **CHECKSUM_WHEN_REQUIRED))


def cloud_user_client(env: Env, user: str):
    """利用者（静的キー。aws-ec2 は IAM ユーザー、gcp-vm は HMAC キー）としてつなぐ S3 互換のクライアント。
    接続先は labtool/clouds の s3_endpoint（AWS は空＝既定、GCP は storage.googleapis.com）。"""
    import boto3
    from botocore.config import Config

    from . import clouds
    cloud = clouds.for_env(env)
    access, secret = user_credentials(env, user)
    return boto3.client("s3", region_name=env.vm_region, aws_access_key_id=access, aws_secret_access_key=secret,
                        endpoint_url=cloud.s3_endpoint(env) or None,
                        config=Config(retries={"max_attempts": 3}, **CHECKSUM_WHEN_REQUIRED))


@contextlib.contextmanager
def admin_s3(env: Env):
    """管理者としてバケットに触る S3 クライアント。Mac は RustFS に port-forward、AWS は操作者の資格情報で S3 に直接。"""
    if env.on_aws:
        from . import aws
        yield aws.client(env, "s3")
        return
    if env.is_gcp_vm:
        from .clouds import gcp
        yield gcp.admin_s3_client(env)
        return
    with port_forward("svc/rustfs", 9000) as endpoint:
        yield s3_client(endpoint, *admin_credentials(env))


@contextlib.contextmanager
def user_s3(env: Env, user: str):
    """利用者の静的キーでつなぐ S3 クライアント（Mac は RustFS の利用者、aws-ec2 は IAM ユーザー。EKS 版には無い）。"""
    if env.is_vm:
        from . import vm
        vm.load_static_keys(env)
        yield cloud_user_client(env, user)
        return
    with port_forward("svc/rustfs", 9000) as endpoint:
        yield s3_client(endpoint, *user_credentials(env, user))


def check_buckets(env: Env, s3, specs: dict[str, dict] | None = None) -> None:
    """AWS: バケット（EKS 版は CDK、aws-ec2 は labtool/clouds/aws_ec2.py が作る）が仕様どおりかを確かめる。
    specs は実名 → 仕様（省くと render.s3_buckets の 4 つを接頭辞付きの名前で）。
    違えばエラーで止める（作り直しは基盤のコードで）。"""
    problems: list[str] = []
    if specs is None:
        specs = {render.bucket_name(env, logical): spec for logical, spec in render.s3_buckets(env).items()
                 if not (logical == render.PG_BACKUP and env.is_aws)}   # Aurora のバックアップは RDS が持つ
    for bucket, spec in specs.items():
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            problems.append(f"{bucket}: バケットがありません")
            continue
        versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
        if versioning != spec["versioning"]:
            problems.append(f"{bucket}: バージョニングが {versioning}（仕様 {spec['versioning']}）")
        locked, rule = _lock_rule(s3, bucket)
        if spec["lock"] and not locked:
            problems.append(f"{bucket}: Object Lock が無効です（後から有効にできないので、バケットを作り直す）")
        elif spec["lock"] and rule != spec["lock"]:
            problems.append(f"{bucket}: Object Lock の既定の保持が {rule}（仕様 {spec['lock']}）")
        have = {(r["ID"], r.get("Expiration", {}).get("Days"), r.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays"))
                for r in _lifecycle_rules(s3, bucket)}
        want = {(r["ID"], r.get("Expiration", {}).get("Days"), r.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays"))
                for r in spec["lifecycle"]}
        if have != want:
            problems.append(f"{bucket}: ライフサイクルが仕様と違います（{sorted(have)} / 仕様 {sorted(want)}）")
        pab = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        if not all(pab.values()):
            problems.append(f"{bucket}: パブリックアクセスの遮断が不完全です")
        info(f"{bucket}: OK")
    if problems:
        raise LabError("S3 のバケットが仕様と違います:\n  " + "\n  ".join(problems))


class Admin:
    """RustFS の管理 API。本文は JSON、署名は SigV4（サービス名 s3）。"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str) -> None:
        from botocore.credentials import Credentials

        self.endpoint = endpoint
        self.cred = Credentials(access_key, secret_key)

    def call(self, method: str, path: str, body=None) -> tuple[int, str]:
        from botocore.auth import S3SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = self.endpoint + "/rustfs/admin/v3" + path
        data = json.dumps(body).encode() if body is not None else b""
        req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
        S3SigV4Auth(self.cred, "s3", REGION).add_auth(req)
        r = urllib.request.Request(url, data=data if method != "GET" else None, method=method,
                                   headers=dict(req.headers.items()))
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def must(self, method: str, path: str, body=None) -> str:
        status, text = self.call(method, path, body)
        if status != 200:
            raise LabError(f"RustFS の管理 API {method} {path} が失敗しました（{status}）: {text[:300]}")
        return text


def admin_credentials(env: Env) -> tuple[str, str]:
    return env.get("RUSTFS_ACCESS_KEY"), env.get("RUSTFS_SECRET_KEY")


def user_credentials(env: Env, user: str) -> tuple[str, str]:
    key_name, secret_name = render.s3_env_names(user)
    return env.get(key_name), env.get(secret_name)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _lifecycle_rules(s3, bucket: str) -> list[dict]:
    from botocore.exceptions import ClientError

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchLifecycleConfiguration", "NoSuchBucketLifecycle"):
            return []
        raise
    return sorted(rules, key=lambda r: r.get("ID", ""))


def _lock_rule(s3, bucket: str) -> tuple[bool, dict | None]:
    from botocore.exceptions import ClientError

    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
    except ClientError:
        return False, None
    enabled = cfg.get("ObjectLockEnabled") == "Enabled"
    return enabled, (cfg.get("Rule") or {}).get("DefaultRetention")


def ensure_buckets(env: Env, s3, specs: dict[str, dict] | None = None, region: str | None = None) -> None:
    """バケットを仕様（実名 → render.s3_buckets の形。省くと Mac の 4 つ）に合わせる。Mac の RustFS も AWS の S3 も同じ関数。
    region は AWS で us-east-1 以外にバケットを作るときの LocationConstraint。"""
    from botocore.exceptions import ClientError

    if specs is None:
        specs = render.s3_buckets(env)
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    for name, spec in specs.items():
        lock = spec["lock"]
        if name not in existing:
            location = {"CreateBucketConfiguration": {"LocationConstraint": region}} if region and region != "us-east-1" else {}
            s3.create_bucket(Bucket=name, **({"ObjectLockEnabledForBucket": True} if lock else {}), **location)
            info(f"バケット {name} を作りました" + ("（Object Lock あり）" if lock else ""))
        if spec["versioning"] and s3.get_bucket_versioning(Bucket=name).get("Status") != "Enabled":
            s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
            info(f"バケット {name} のバージョニングを有効にしました")
        if lock:
            enabled, current = _lock_rule(s3, name)
            if not enabled:
                raise LabError(f"バケット {name} は Object Lock が無効のまま作られています。後から有効にはできないので、"
                               "./lab rebuild で作り直してください")
            if current != lock:
                s3.put_object_lock_configuration(Bucket=name, ObjectLockConfiguration={
                    "ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": lock}})
                info(f"バケット {name} の既定の保持を {lock['Mode']} {lock['Days']} 日にしました")
        want = sorted(spec["lifecycle"], key=lambda r: r["ID"])
        if _canon(_lifecycle_rules(s3, name)) != _canon(want):
            if want:
                s3.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration={"Rules": want})
            else:
                try:
                    s3.delete_bucket_lifecycle(Bucket=name)
                except ClientError:
                    pass
            info(f"バケット {name} のライフサイクルを更新しました")
    ok("バケットの設定は最新です")


def ensure_users(env: Env, admin: Admin) -> None:
    users = json.loads(admin.must("GET", "/list-users") or "{}")
    policies = render.s3_policies()
    for user in render.S3_USERS:
        access, secret = user_credentials(env, user)
        if access not in users:
            admin.must("PUT", f"/add-user?accessKey={access}", {"secretKey": secret, "status": "enabled"})
            info(f"利用者 {access} を作りました")
        policy_name = f"lab-{user}"
        status, text = admin.call("GET", f"/info-canned-policy?name={policy_name}")
        current = (json.loads(text).get("policy") if status == 200 and text else None)
        if _canon(current) != _canon(policies[user]):
            admin.must("PUT", f"/add-canned-policy?name={policy_name}", policies[user])
            info(f"権限 {policy_name} を更新しました")
        attached = (users.get(access) or {}).get("policyName")
        if attached != policy_name:
            admin.must("PUT", f"/set-user-or-group-policy?policyName={policy_name}&userOrGroup={access}&isGroup=false")
            info(f"利用者 {access} に権限 {policy_name} を割り当てました")
    ok("利用者と権限は最新です")


def self_check(env: Env, admin_client, user_client) -> None:
    """許した操作ができ、許していない操作（原本の読み取り・削除、保持の回避なしの削除）が拒否されることを確かめる。
    admin_client は管理者の S3 クライアント、user_client(利用者名) は利用者の静的キーのクライアント
    （Mac は RustFS、aws-ec2 は IAM ユーザー。両環境で同じ検査）。"""
    from botocore.exceptions import ClientError

    def denied(fn, codes=("AccessDenied",)) -> bool:
        try:
            fn()
        except ClientError as e:
            return e.response.get("Error", {}).get("Code") in codes
        return False

    raw = render.bucket_name(env, render.AUDIT_RAW)
    writer = user_client("litellm-writer")
    viewer = user_client("audit-viewer")
    key = f"raw/_selfcheck/{time.strftime('%Y%m%dT%H%M%S')}.txt"
    version = writer.put_object(Bucket=raw, Key=key, Body=b"self-check").get("VersionId")
    checks = {
        "書き込み用の利用者は原本を読めない": denied(lambda: writer.get_object(Bucket=raw, Key=key)),
        "書き込み用の利用者は原本を消せない": denied(
            lambda: writer.delete_object(Bucket=raw, Key=key, VersionId=version)),
        "書き込み用の利用者は raw/ の外に書けない": denied(
            lambda: writer.put_object(Bucket=raw, Key="_selfcheck.txt", Body=b"x")),
        # S3 と RustFS はオブジェクトロックの拒否を AccessDenied で、GCS は保持ポリシーの拒否を RetentionPolicyNotMet で返す
        "管理者でも保持の回避なしには原本を消せない": denied(
            lambda: admin_client.delete_object(Bucket=raw, Key=key, VersionId=version),
            codes=("AccessDenied", "RetentionPolicyNotMet")),
        "検索アプリは原本に書けない": denied(
            lambda: viewer.put_object(Bucket=raw, Key="raw/_selfcheck/viewer.txt", Body=b"x")),
        "検索アプリは原本を読める": not denied(lambda: viewer.get_object(Bucket=raw, Key=key)),
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        raise LabError("オブジェクトストレージの自己確認が失敗しました: " + "、".join(failed))
    ok(f"オブジェクトストレージの自己確認: {len(checks)} 件すべて期待どおり")


def snapshot(env: Env, s3) -> list[str]:
    """冪等性の試験用: バケットの設定と、利用者と権限を文字列の一覧にする。
    AWS では利用者は IAM（CDK か CloudFormation が管理）なので、バケットの設定だけ。"""
    lines = []
    for logical in sorted(render.s3_buckets(env)):
        if env.is_aws and logical == render.PG_BACKUP:
            continue
        name = render.bucket_name(env, logical)
        lock = _lock_rule(s3, name)
        lines.append(f"bucket {name} versioning={s3.get_bucket_versioning(Bucket=name).get('Status')} "
                     f"lock={_canon(lock)} lifecycle={_canon(_lifecycle_rules(s3, name))}")
    if env.on_aws:
        return lines
    endpoint = s3.meta.endpoint_url
    admin = Admin(endpoint, *admin_credentials(env))
    users = json.loads(admin.must("GET", "/list-users") or "{}")
    for access, meta in sorted(users.items()):
        lines.append(f"s3user {access} policy={meta.get('policyName')} status={meta.get('status')}")
    for user in render.S3_USERS:
        _, text = admin.call("GET", f"/info-canned-policy?name=lab-{user}")
        lines.append(f"s3policy lab-{user} {_canon(json.loads(text).get('policy') if text else None)}")
    return lines
