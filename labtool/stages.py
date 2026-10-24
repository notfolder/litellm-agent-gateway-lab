"""構築の各段階。どの段階も、何度実行しても同じ状態になる（docs/design.md 17 章）。"""

from __future__ import annotations

import json
import shutil
import sys

from . import aws
from . import objectstore as objstore
from . import render
from .core import (
    BUILD_DIR, CACHE_HOST_PORT, CACHE_NAME, CLUSTER, ENV_EXAMPLE, ENV_FILE, HELM_RELEASE, K3S_IMAGE,
    LITELLM_CHART, NS, REGISTRY_HOST_PORT, REGISTRY_IN_CLUSTER, REGISTRY_NAME, ROOT, Env, LabError,
    admin_api, cluster_exists, configmap_manifest, container_running, gen_key, gen_secret, helm, http,
    http_ok, info, json_output, kapply, kc, kn, ok, output, port_forward, registry_exists, render_template,
    require_commands, rollout, run, secret_manifest, short_hash, step, succeeds, wait_until, warn,
    write_if_changed,
)

K8S = ROOT / "k8s"
IMAGE_FILE = BUILD_DIR / "litellm-image.json"
AUDIT_IMAGE_FILE = BUILD_DIR / "audit-image.json"

# クラスタに入れる部品の版（変更設計書 3.1 節）
CERT_MANAGER = {"repo": "https://charts.jetstack.io", "chart": "cert-manager", "version": "v1.21.2"}
# クラスタ全体の部品（オペレーター、証明書、入口）は server ノード（Kubernetes の制御部分）に置く。障害の試験で止めるのは
# agent ノードだけなので、試験で止まらない（2026-10-12 に、CloudNativePG のオペレーターが止めたノードにあって、
# ノードが戻るまで切り替えが始まらなかった）。本番では、これらを複数台にする
CONTROL_PLANE = {"node-role.kubernetes.io/control-plane": "true"}
CNPG = {"repo": "https://cloudnative-pg.github.io/charts", "chart": "cloudnative-pg", "version": "0.29.1"}
BARMAN_PLUGIN_VERSION = "v0.15.1"
BARMAN_PLUGIN_URL = ("https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/"
                     f"{BARMAN_PLUGIN_VERSION}/manifest.yaml")
# AWS 環境だけ: Secrets Manager → Secret の同期（External Secrets Operator。変更仕様書 2026-10-24-AWS構成 0.2 節）
ESO = {"repo": "https://charts.external-secrets.io", "chart": "external-secrets", "version": "2.14.0"}
USER_KEYS = ["LAB_KEY_USER1_CLAUDE", "LAB_KEY_USER1_CODEX", "LAB_KEY_USER2_CLAUDE", "LAB_KEY_USER2_CODEX"]
# 審査を外したキー（ユーザー 1 の検証用。LAB_NOREVIEW_KEYS=true のときだけ作る。docs/manual-check.md）
NOREVIEW_KEYS = ["LAB_KEY_USER1_CLAUDE_NOREVIEW", "LAB_KEY_USER1_CODEX_NOREVIEW"]


def preflight() -> Env:
    """前提の確認。何も変更しない（.env が無いときに雛形からコピーすることだけを行う）。"""
    step("preflight 前提の確認")
    if not ENV_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        ENV_FILE.chmod(0o600)
        info(".env を .env.example から作りました")
    env = Env.load()
    if env.is_aws:
        _preflight_aws(env)
    else:
        require_commands("docker", "k3d", "kubectl", "helm")
        if not succeeds(["kubectl", "cnpg", "version"]):
            raise LabError("kubectl cnpg（CloudNativePG のプラグイン）がありません。brew install kubectl-cnpg で入れてください")
    if not succeeds(["docker", "info"]):
        raise LabError("Docker が動いていません。OrbStack を起動してください")

    # 外付け SSD（LAB_DATA_DIR の親）がつながっているか
    if not env.data_dir.parent.is_dir():
        raise LabError(f"LAB_DATA_DIR の親フォルダ {env.data_dir.parent} がありません"
                       "（外付け SSD がつながっているか確かめてください）")
    (env.data_dir / "chatgpt").mkdir(parents=True, exist_ok=True)

    if not env.is_aws:
        port_busy = succeeds(["lsof", "-nP", f"-iTCP:{env.http_port}", "-sTCP:LISTEN"])
        if port_busy and not cluster_exists():
            raise LabError(f"ポート {env.http_port} が別のプロセスに使われています（.env の LAB_HTTP_PORT を変えてください）")

    # 手作業の準備の状況（無くても構築は進む。該当する機能だけ無効になる）
    for present, good, bad in [
        (env.has_google(), "Google の OAuth クライアント: 設定あり",
         "Google の OAuth クライアントが未設定です。SSO と Gmail の MCP は無効のまま構築します（docs/setup-google.md）"),
        (env.has_users(), "検証ユーザー: 設定あり", "LAB_USER1_EMAIL が未設定です。利用者のキーは作りません"),
        (env.has_jev(), "Jev の API キー: 設定あり", "TYPESAFE_API_KEY が未設定です。一次判定（Jev）を使わずに構築します"),
        (env.has_chatgpt(), "ChatGPT のトークン: あり",
         "ChatGPT のトークンがありません。./lab chatgpt-login で取得するまで、Codex と二次判定は動きません"),
    ]:
        ok(good) if present else warn(bad)
    ok(f"前提の確認が終わりました（LAB_TARGET={env.target}）")
    return env


def _preflight_aws(env: Env) -> None:
    """AWS 環境の前提: 道具（aws、npx、kubectl、helm）、資格情報、ホスト名。バケット名の接頭辞を Env に載せる。"""
    require_commands("docker", "kubectl", "helm", "aws", "npx")
    _ = env.aws_hostname   # 無ければここで止まる
    ident = aws.identity(env)
    env.values["LAB_AWS_ACCOUNT"] = ident["Account"]
    env.values["LAB_AWS_BUCKET_PREFIX"] = render.bucket_prefix(env, ident["Account"])
    ok(f"AWS の資格情報: アカウント {ident['Account']}、{ident['Arn']}（{env.aws_region}）")
    if not (aws.CDK_DIR / "cdk.json").exists():
        raise LabError("cdk/ がありません（AWS の基盤の定義）")


def ensure_registry(name: str, port: int, *extra: str) -> None:
    """イメージの置き場が無ければ作り、止まっていれば起動する。"""
    if registry_exists(name):
        if not container_running(f"k3d-{name}"):
            run(["docker", "start", f"k3d-{name}"], quiet=True)
        ok(f"イメージ置き場 k3d-{name} は作成済みです")
    else:
        info(f"イメージ置き場 k3d-{name} を作ります")
        run(["k3d", "registry", "create", name, "--port", f"127.0.0.1:{port}", *extra])
    wait_until(60, f"k3d-{name} の応答", lambda: http_ok(f"http://localhost:{port}/v2/"))


def cluster(env: Env) -> None:
    """Mac: イメージの置き場 2 つ、k3d クラスタ、Traefik の設定、namespace。
    AWS: CDK の配備（証明書、ネットワーク、データ、計算資源）、kubeconfig、ノードプール、namespace、
    External Secrets Operator、EFS の StorageClass（変更仕様書 2026-10-24-AWS構成 2.3 節）。"""
    step("cluster クラスタ")
    if env.is_aws:
        _cluster_aws(env)
        return
    # イメージの置き場はクラスタより先に作る。クラスタを消しても残す
    ensure_registry(REGISTRY_NAME, REGISTRY_HOST_PORT)
    ensure_registry(CACHE_NAME, CACHE_HOST_PORT, "--proxy-remote-url", "https://registry-1.docker.io")

    cfg = BUILD_DIR / "k3d-config.yaml"
    write_if_changed(cfg, render_template(K8S / "k3d-config.yaml", {
        "K3S_IMAGE": K3S_IMAGE, "LAB_HTTP_PORT": env.http_port, "LAB_DATA_DIR": env.data_dir,
        "LAB_REGISTRY_NAME": REGISTRY_NAME, "LAB_CACHE_NAME": CACHE_NAME,
    }))

    server = f"k3d-{CLUSTER}-server-0"
    if cluster_exists():
        # 既存のクラスタが設定と食い違っていないか（ノード数、k3s の版、イメージの置き場）
        nodes = json_output(["k3d", "node", "list", "-o", "json"], default=[]) or []
        count = sum(1 for n in nodes if n.get("runtimeLabels", {}).get("k3d.cluster") == CLUSTER
                    and n.get("role") in ("server", "agent"))
        image = output(["docker", "inspect", server, "--format", "{{.Config.Image}}"], check=False).strip()
        if count != 3 or image != K3S_IMAGE:
            raise LabError(f"既存のクラスタ（ノード {count}、{image}）が設定（ノード 3、{K3S_IMAGE}）と違います。"
                           "./lab rebuild で作り直してください")
        if not succeeds(["docker", "exec", server, "grep", "-q", CACHE_NAME, "/etc/rancher/k3s/registries.yaml"]):
            raise LabError("既存のクラスタがイメージ置き場を使う設定になっていません。./lab rebuild で作り直してください")
        if not container_running(server):
            info("停止中のクラスタを起動します")
            run(["k3d", "cluster", "start", CLUSTER])
        ok(f"クラスタ {CLUSTER} は作成済みです")
    else:
        info(f"クラスタ {CLUSTER} を作ります")
        run(["k3d", "cluster", "create", "--config", str(cfg)])

    wait_until(180, "全ノードの Ready", kc("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=5s"))

    kapply(files=[K8S / "traefik-config.yaml"], namespaced=False)
    wait_until(300, "Traefik の起動", kc("-n", "kube-system", "rollout", "status", "deploy/traefik", "--timeout=5s"))
    args = output(kc("-n", "kube-system", "get", "deploy", "traefik",
                     "-o", "jsonpath={.spec.template.spec.containers[0].args}"), check=False)
    if "readTimeout=600s" not in args:
        warn("Traefik の起動引数にタイムアウトの設定が見えません（反映待ちの可能性があります）")

    kapply(files=[K8S / "namespace.yaml"], namespaced=False)
    kapply(manifest=render.service_accounts_manifest())
    operators()
    ok("クラスタの準備ができました")


def _cluster_aws(env: Env) -> None:
    ident = aws.identity(env)
    out = aws.deploy(env, ident)
    aws.kubeconfig(env)
    wait_until(120, "EKS の API サーバーへの接続", kc("get", "--raw", "/healthz"))
    kapply(manifest=render.nodepool_manifest(env), namespaced=False)
    kapply(files=[K8S / "namespace.yaml"], namespaced=False)
    kapply(manifest=render.service_accounts_manifest())
    # Secrets Manager → Secret の同期。コントローラーの ServiceAccount に Pod Identity で IAM ロールが付く
    _ensure_chart("external-secrets", render.ESO_NAMESPACE, ESO, {
        "installCRDs": True, "serviceAccount": {"name": aws.POD_IDENTITIES["external-secrets"][1]},
        "webhook": {"replicaCount": 1}, "certController": {"replicaCount": 1},
    })
    wait_until(300, "External Secrets Operator の準備", kc("-n", render.ESO_NAMESPACE, "wait",
                                                          "--for=condition=Available", "deploy", "--all", "--timeout=5s"))
    kapply(manifest=render.eso_store_manifest(env), namespaced=False)
    kapply(manifest=render.efs_manifests(out), namespaced=False)
    ok(f"クラスタの準備ができました（EKS {env.aws_cluster_name}、{env.aws_region}）")


def _ensure_chart(release: str, namespace: str, spec: dict, values: dict) -> None:
    """Helm のリリースを入れる。値のハッシュが同じなら helm を流さない（流すたびにリビジョンが増えるのを防ぐ）。
    ハッシュはリリースの説明（--description）に持つ（cert-manager のチャートは、値に知らない項目があると断るため）。"""
    values_hash = short_hash(json.dumps([spec, values], sort_keys=True))
    mark = f"labValuesHash={values_hash}"
    history = json_output(helm("-n", namespace, "history", release, "--max", "1", "-o", "json"), default=[]) or []
    if history and history[-1].get("status") == "deployed" and history[-1].get("description") == mark:
        ok(f"{release} は最新です（{spec['version']}）")
        return
    info(f"{release} {spec['version']} を入れます")
    values_file = BUILD_DIR / f"{release}-values.json"
    write_if_changed(values_file, json.dumps(values, indent=2) + "\n")
    run(helm("upgrade", "--install", release, spec["chart"], "--repo", spec["repo"], "--version", spec["version"],
             "-n", namespace, "--create-namespace", "-f", str(values_file), "--description", mark,
             "--wait", "--timeout", "10m"))


def operators() -> None:
    """cert-manager → CloudNativePG のオペレーター → Barman Cloud Plugin（この順。プラグインは証明書を使う）。"""
    _ensure_chart("cert-manager", "cert-manager", CERT_MANAGER, {
        "crds": {"enabled": True}, "nodeSelector": CONTROL_PLANE, "webhook": {"nodeSelector": CONTROL_PLANE},
        "cainjector": {"nodeSelector": CONTROL_PLANE}, "startupapicheck": {"nodeSelector": CONTROL_PLANE}})
    wait_until(300, "cert-manager の準備", kc("-n", "cert-manager", "wait", "--for=condition=Available",
                                              "deploy", "--all", "--timeout=5s"))
    _ensure_chart("cnpg", "cnpg-system", CNPG, {"nodeSelector": CONTROL_PLANE})
    wait_until(300, "CloudNativePG のオペレーターの準備", kc("-n", "cnpg-system", "wait", "--for=condition=Available",
                                                         "deploy", "-l", "app.kubernetes.io/name=cloudnative-pg",
                                                         "--timeout=5s"))
    manifest = BUILD_DIR / f"barman-cloud-{BARMAN_PLUGIN_VERSION}-pinned.yaml"
    if not manifest.exists():
        info(f"Barman Cloud Plugin {BARMAN_PLUGIN_VERSION} のマニフェストを取得します")
        res = http("GET", BARMAN_PLUGIN_URL, timeout=60)
        if res.status != 200:
            raise LabError(f"Barman Cloud Plugin のマニフェストを取得できませんでした（{res.status}）")
        write_if_changed(manifest, render.pin_deployments(res.body, CONTROL_PLANE))
    kapply(files=[manifest], namespaced=False)
    wait_until(300, "Barman Cloud Plugin の準備", kc("-n", "cnpg-system", "rollout", "status", "deploy/barman-cloud",
                                                  "--timeout=5s"))


def _build_and_push(name: str, tag: str, context, build_args: list[str], env: Env | None = None) -> dict:
    """自作イメージをビルドして置き場に push する（同じタグがあれば、どちらも飛ばす）。"""
    local_image = f"{name}:{tag}"
    push_image = f"localhost:{REGISTRY_HOST_PORT}/{name}:{tag}"
    if succeeds(["docker", "image", "inspect", local_image]):
        ok(f"イメージ {local_image} はビルド済みです")
    else:
        info(f"イメージ {local_image} をビルドします（単体テストも流れます）")
        run(["docker", "build", *build_args, "-t", local_image, str(context)])
    accept = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, "
              "application/vnd.oci.image.manifest.v1+json")
    res = http("HEAD", f"http://localhost:{REGISTRY_HOST_PORT}/v2/{name}/manifests/{tag}",
               headers={"Accept": accept}, timeout=10)
    if res.status == 200:
        ok(f"置き場に {name}:{tag} があります")
    else:
        info(f"置き場に {name}:{tag} を push します")
        run(["docker", "tag", local_image, push_image])
        # Mac 側には arm64 の中身しか無いので、arm64 だけを push する
        run(["docker", "push", "--platform", "linux/arm64", "-q", push_image], quiet=True)
        ok(f"push しました: {push_image}")
    # クラスタの中から見たイメージ名（マニフェストと Helm に渡す）
    return {"repository": f"{REGISTRY_IN_CLUSTER}/{name}", "tag": tag, "local": local_image}


def images(env: Env) -> dict:
    """検証用 LiteLLM イメージと、監査ログの整形・検索アプリのイメージ（audit-lab）のビルドと push。
    - 中身のハッシュをタグにする。同じタグがあればビルドしない。置き場に同じタグがあれば push しない
    - apps/audit が無い間（実装の途中）は audit-lab を飛ばす
    外部のイメージ（PostgreSQL など）は、ノードが Docker Hub の中継キャッシュ経由で取るので扱わない。"""
    step("images イメージ")
    push = _build_and_push_ecr if env.is_aws else _build_and_push
    tag = render.image_tag(env.litellm_base_image, ROOT / "litellm")
    image = push("litellm-lab", tag, ROOT / "litellm",
                 ["--build-arg", f"LITELLM_BASE_IMAGE={env.litellm_base_image}"], env=env)
    write_if_changed(IMAGE_FILE, json.dumps(image, indent=2) + "\n")
    if (ROOT / "apps" / "audit" / "Dockerfile").exists():
        audit = push("audit-lab", render.dir_hash(ROOT / "apps" / "audit"), ROOT / "apps" / "audit", [], env=env)
        write_if_changed(AUDIT_IMAGE_FILE, json.dumps(audit, indent=2) + "\n")
    return image


def _build_and_push_ecr(name: str, tag: str, context, build_args: list[str], env: Env) -> dict:
    """AWS: 同じイメージ（linux/arm64）を ECR に push する。リポジトリは CDK が作る（{prefix}/<name>）。"""
    local_image = f"{name}:{tag}"
    if succeeds(["docker", "image", "inspect", local_image]):
        ok(f"イメージ {local_image} はビルド済みです")
    else:
        info(f"イメージ {local_image} をビルドします（単体テストも流れます）")
        run(["docker", "build", "--platform", "linux/arm64", *build_args, "-t", local_image, str(context)])
    out = aws.load_outputs(env)
    repository = f"{env.aws_prefix}/{name}"
    registry = out["EcrRegistry"]
    if aws.ecr_has_tag(env, repository, tag):
        ok(f"ECR に {repository}:{tag} があります")
    else:
        info(f"ECR に {repository}:{tag} を push します")
        aws.ecr_login(env, registry)
        push_image = f"{registry}/{repository}:{tag}"
        run(["docker", "tag", local_image, push_image])
        run(["docker", "push", "-q", push_image], quiet=True)
        ok(f"push しました: {push_image}")
    return {"repository": f"{registry}/{repository}", "tag": tag, "local": local_image}


def load_image() -> dict:
    if not IMAGE_FILE.exists():
        raise LabError("イメージ名がありません。先に ./lab images を流してください")
    return json.loads(IMAGE_FILE.read_text())


def secrets_(env: Env) -> Env:
    """Mac: .env の空の値を生成し、k8s の Secret に反映する。既存の値は上書きしない。
    AWS: Secrets Manager の {prefix}/app の空の項目を生成し、External Secrets Operator に同じ名前の Secret を作らせる。"""
    step("secrets 秘密情報")
    if env.is_aws:
        _secrets_aws(env)
        return env
    env.fill("LITELLM_MASTER_KEY", gen_key())
    env.fill("LITELLM_SALT_KEY", gen_key())
    env.fill("POSTGRES_PASSWORD", gen_secret(16))
    env.fill("REDIS_PASSWORD", gen_secret(16))
    env.fill("SENTINEL_PASSWORD", gen_secret(16))
    for k in USER_KEYS:
        env.fill(k, gen_key())
    if render.noreview_enabled(env):
        for k in NOREVIEW_KEYS:
            env.fill(k, gen_key())
    # オブジェクトストレージの管理者と、用途ごとの利用者（アクセスキーは利用者の名前、シークレットは生成）
    env.fill("RUSTFS_ACCESS_KEY", "lab-admin")
    env.fill("RUSTFS_SECRET_KEY", gen_secret(20))
    for user in render.S3_USERS:
        key_name, secret_name = render.s3_env_names(user)
        env.fill(key_name, user)
        env.fill(secret_name, gen_secret(20))
    env.fill("AUDIT_SESSION_SECRET", gen_secret(32))

    pg_password = env.get("POSTGRES_PASSWORD")
    # CloudNativePG の初期化に渡す（型は basic-auth）。パスワードは .env の値を使い回し、作り直しても変わらない
    kapply(manifest=secret_manifest("litellm-pg-app", {"username": "litellm", "password": pg_password},
                                    "kubernetes.io/basic-auth"))
    # DB 移行の Job が使う直結の接続先（Pooler を通さない）
    kapply(manifest=secret_manifest("litellm-migrate-db", {
        "DATABASE_URL": f"postgresql://litellm:{pg_password}@litellm-pg-rw:5432/litellm"}))
    backup_key, backup_secret = render.s3_env_names("pg-backup")
    kapply(manifest=secret_manifest("pg-backup-s3", {"ACCESS_KEY_ID": env.get(backup_key),
                                                     "ACCESS_SECRET_KEY": env.get(backup_secret)}))
    for user, secret in (("audit-compactor", "audit-compactor-s3"), ("audit-viewer", "audit-viewer-s3")):
        key_name, secret_name = render.s3_env_names(user)
        kapply(manifest=secret_manifest(secret, {"AUDIT_S3_ACCESS_KEY": env.get(key_name),
                                                 "AUDIT_S3_SECRET_KEY": env.get(secret_name)}))
    kapply(manifest=secret_manifest("audit-viewer-session", {"AUDIT_SESSION_SECRET": env.get("AUDIT_SESSION_SECRET")}))
    oidc_id, oidc_secret = render.audit_oidc_client(env)
    if oidc_id:
        kapply(manifest=secret_manifest("audit-viewer-oidc", {"AUDIT_OIDC_CLIENT_ID": oidc_id,
                                                              "AUDIT_OIDC_CLIENT_SECRET": oidc_secret}))
    kapply(manifest=secret_manifest("valkey", {"password": env.get("REDIS_PASSWORD"),
                                               "sentinel-password": env.get("SENTINEL_PASSWORD")}))
    kapply(manifest=secret_manifest("litellm-secrets", render.secrets_for_litellm(env)))
    kapply(manifest=secret_manifest("rustfs", {"access-key": env.get("RUSTFS_ACCESS_KEY"),
                                               "secret-key": env.get("RUSTFS_SECRET_KEY")}))
    if env.has_google():
        kapply(manifest=secret_manifest("workspace-mcp", {
            "GOOGLE_OAUTH_CLIENT_ID": env.get("GOOGLE_OAUTH_CLIENT_ID"),
            "GOOGLE_OAUTH_CLIENT_SECRET": env.get("GOOGLE_OAUTH_CLIENT_SECRET"),
        }))
    ok("Secret を反映しました")
    return env


def _secrets_aws(env: Env) -> None:
    aws.ensure_app_secret(env)
    out = aws.load_outputs(env)
    kapply(manifest=render.external_secrets_manifest(env, out))

    def synced() -> bool:
        items = (json_output(kn("get", "externalsecret", "-o", "json"), default={}) or {}).get("items", [])
        if not items:
            return False
        for es in items:
            conds = {c["type"]: c["status"] for c in (es.get("status") or {}).get("conditions", [])}
            if conds.get("Ready") != "True":
                return False
        return True

    wait_until(180, "Secrets Manager から Secret への同期", synced)
    ok("Secret を反映しました（External Secrets Operator）")


def objectstore(env: Env) -> None:
    """S3 互換のオブジェクトストレージ（RustFS）と、バケット、利用者、権限（変更設計書 2 章）。
    最後に、許した操作ができ、許していない操作が拒否されることを確かめる。"""
    step("objectstore オブジェクトストレージ")
    if env.is_aws:
        # バケット、ロック、ライフサイクル、IAM は CDK が作る。ここは仕様どおりかの検査だけ（変更仕様書 2.4 節）
        objstore.check_buckets(env, aws.client(env, "s3"))
        ok("オブジェクトストレージ（S3）は仕様どおりです")
        return
    kapply(files=[K8S / "rustfs.yaml"])
    rollout("statefulset/rustfs", 180, "RustFS の起動")
    with port_forward("svc/rustfs", 9000) as endpoint:
        s3 = objstore.s3_client(endpoint, *objstore.admin_credentials(env))

        def ready() -> bool:
            try:
                s3.list_buckets()
                return True
            except Exception:  # noqa: BLE001 起動直後は応答しないことがある
                return False

        wait_until(60, "RustFS の S3 の応答", ready)
        objstore.ensure_buckets(env, s3)
        objstore.ensure_users(env, objstore.Admin(endpoint, *objstore.admin_credentials(env)))
        objstore.self_check(env, endpoint)
    ok("オブジェクトストレージの準備ができました")


def _delete_if_present(*resources: str) -> None:
    """以前の構成の部品を消す。無ければ何もしない（2 回目以降に流しても失敗しない）。"""
    for r in resources:
        if succeeds(kn("get", r)):
            info(f"以前の構成の {r} を消します")
            run(kn("delete", r, "--ignore-not-found", "--wait=true"))


def datastores(env: Env) -> None:
    """PostgreSQL（CloudNativePG、3 台）と Redis。差分だけ反映し、準備ができるまで待つ。"""
    step("datastores PostgreSQL と Redis")
    if env.is_aws:
        # Aurora と ElastiCache は CDK が作る。ここは使える状態かの確認だけ
        out = aws.load_outputs(env)
        rds = aws.client(env, "rds").describe_db_clusters(DBClusterIdentifier=out["AuroraClusterIdentifier"])
        status = rds["DBClusters"][0]["Status"]
        if status != "available":
            raise LabError(f"Aurora {out['AuroraClusterIdentifier']} が available ではありません（{status}）")
        g = aws.client(env, "elasticache").describe_replication_groups(
            ReplicationGroupId=out["ValkeyReplicationGroupId"])["ReplicationGroups"][0]
        if g["Status"] != "available":
            raise LabError(f"ElastiCache {out['ValkeyReplicationGroupId']} が available ではありません（{g['Status']}）")
        ok(f"Aurora（writer {aws.aurora_writer(env, out)}）と ElastiCache for Valkey"
           f"（primary {aws.valkey_primary(env, out)}、durability {g.get('EffectiveDurability', '?')}）は使えます")
        return
    # 1 台の PostgreSQL（以前の構成）を消す。中身は引き継がない（変更仕様書 4 章）
    _delete_if_present("statefulset/postgres", "service/postgres", "pvc/data-postgres-0", "secret/postgres")
    kapply(manifest=render_template(K8S / "postgres-cnpg.yaml", {"S3_ENDPOINT": env.get("LAB_S3_ENDPOINT",
                                                                                       render.DEFAULT_S3_ENDPOINT)}))
    wait_until(1200, "PostgreSQL（3 台）の準備", kn("wait", "cluster/litellm-pg", "--for=condition=Ready",
                                                  "--timeout=5s"))
    rollout("deploy/litellm-pg-pooler-rw", 180, "Pooler の起動")

    # Valkey と Sentinel（変更設計書 4 章）。1 台の Redis（以前の構成）は消す（中身はキャッシュと回数の集計だけ）
    _delete_if_present("deployment/redis", "service/redis", "secret/redis")
    scripts = {p.name: p.read_text() for p in sorted((K8S / "valkey").glob("*.sh"))}
    kapply(manifest=configmap_manifest("valkey-scripts", scripts))
    kapply(manifest=render_template(K8S / "valkey.yaml", {"SCRIPTS_HASH": short_hash(json.dumps(scripts,
                                                                                                sort_keys=True))}))
    rollout("statefulset/valkey", 300, "Valkey（3 台）の起動")
    rollout("statefulset/valkey-sentinel", 300, "Sentinel（3 台）の起動")
    wait_until(60, "Sentinel 3 台が同じ primary を指す", lambda: len(set(sentinel_masters(env))) == 1
               and len(sentinel_masters(env)) == 3)
    ok(f"PostgreSQL と Valkey の準備ができました（Valkey の primary: {sentinel_masters(env)[0].split('.')[0]}）")


def sentinel_masters(env: Env) -> list[str]:
    """3 台の Sentinel それぞれが答える、今の primary（答えなければ含めない）。"""
    answers = []
    for i in range(3):
        out = output(kn("exec", f"valkey-sentinel-{i}", "--", "valkey-cli", "-p", "26379", "-a",
                        env.get("SENTINEL_PASSWORD"), "--no-auth-warning", "SENTINEL", "get-master-addr-by-name",
                        "mymaster"), check=False).split()
        if out:
            answers.append(out[0])
    return answers


def mcp(env: Env) -> None:
    """試験用の偽 LLM（fake-llm）と、MCP サーバー（google_workspace_mcp。Google の準備があるときだけ）。"""
    step("mcp MCP サーバーと試験用の偽 LLM")
    server = (ROOT / "tests" / "fake-llm" / "server.py").read_text()
    kapply(manifest=configmap_manifest("fake-llm-server", {"server.py": server}))
    # 中身のハッシュを注釈に入れる（変わったときだけ再起動される）
    kapply(manifest=render_template(K8S / "fake-llm.yaml", {"FAKE_LLM_HASH": short_hash(server)}))
    rollout("deploy/fake-llm", 180, "偽 LLM の起動")

    if env.has_google():
        kapply(files=[K8S / "workspace-mcp.yaml"])
        rollout("deploy/workspace-mcp", 300, "google_workspace_mcp の起動")
    else:
        # 設定が消えた場合に備えて、置いてあれば片付ける
        if succeeds(kn("get", "deploy", "workspace-mcp")):
            run(kn("delete", "-f", str(K8S / "workspace-mcp.yaml"), "--ignore-not-found"))
        warn("Google の OAuth クライアントが未設定なので、google_workspace_mcp は置きません")
    ok("MCP サーバーの段階が終わりました")


def _helm_status() -> str:
    data = json_output(helm("-n", NS, "status", HELM_RELEASE, "-o", "json"), default={}) or {}
    return data.get("info", {}).get("status", "")


def _recover_helm_release() -> None:
    """前回の helm が途中で止まり、リリースが pending のまま残っていれば片付ける
    （そのままだと次の helm が失敗する）。"""
    status = _helm_status()
    if status == "pending-install":
        warn(f"途中で止まったインストール（{status}）を削除してから入れ直します")
        run(helm("-n", NS, "uninstall", HELM_RELEASE, "--wait"))
    elif status in ("pending-upgrade", "pending-rollback"):
        warn(f"途中で止まった更新（{status}）を直前の版に戻してから入れ直します")
        if run(helm("-n", NS, "rollback", HELM_RELEASE, "--wait", "--timeout", "10m"), check=False).returncode:
            warn("戻せなかったので削除して入れ直します")
            run(helm("-n", NS, "uninstall", HELM_RELEASE, "--wait"))
    elif status == "failed":
        warn("前回の helm が失敗しています。入れ直します")


def litellm(env: Env) -> None:
    """プロキシ設定の生成、LiteLLM（公式 Helm チャート）、入口（Traefik）。
    helm upgrade --install は、値が同じなら Pod を作り直さない。値のハッシュが同じなら helm 自体を流さない。"""
    step("litellm LiteLLM")
    image = load_image()
    out = aws.load_outputs(env) if env.is_aws else None
    if env.is_aws and env.has_chatgpt():
        # chatgpt/ のトークンは Mac の auth.json を、全 Pod で共有する EFS の PVC に写す（変更仕様書 4.2 節）
        aws.sync_chatgpt_token(env)

    config_text = render.to_yaml(render.litellm_config(env, env.has_chatgpt()), "./lab が生成。手で編集しない")
    if write_if_changed(BUILD_DIR / "litellm-config.yaml", config_text):
        info("プロキシ設定を更新しました")
    kapply(manifest=configmap_manifest("litellm-config", {"config.yaml": config_text}))

    overrides = render.helm_overrides(env, image["repository"], image["tag"], short_hash(config_text), out)
    overrides_file = BUILD_DIR / "litellm-overrides.json"
    write_if_changed(overrides_file, json.dumps(overrides, indent=2) + "\n")
    values_file = ROOT / "litellm" / "values.yaml"
    values_hash = short_hash(values_file.read_text() + overrides_file.read_text() + env.chart_version)

    migrate_db(image, aws.database_resource_id(env, out) if env.is_aws else None)
    _recover_helm_release()
    current = (json_output(helm("-n", NS, "get", "values", HELM_RELEASE, "-o", "json"), default={}) or {})
    if current.get("labValuesHash") == values_hash and _helm_status() == "deployed":
        ok(f"LiteLLM のリリースは最新です（値のハッシュ {values_hash}）")
    else:
        info("LiteLLM を helm upgrade --install します")
        run(helm("upgrade", "--install", HELM_RELEASE, LITELLM_CHART, "--version", env.chart_version,
                 "-n", NS, "-f", str(values_file), "-f", str(overrides_file),
                 "--set-string", f"labValuesHash={values_hash}", "--wait", "--timeout", "15m"))

    rollout("deploy/litellm", 600, "LiteLLM の起動")
    if env.is_aws:
        _ingress_aws(env, out, viewer=bool(render.audit_oidc_client(env)[0]))
    else:
        kapply(files=[K8S / "ingress.yaml"])
    wait_until(120, f"入口からの応答（{env.base_url}/health/liveliness）",
               lambda: http_ok(f"{env.base_url}/health/liveliness"))
    ok(f"LiteLLM の準備ができました: {env.base_url}（管理画面 {env.base_url}/ui）")


def _ingress_aws(env: Env, out: dict, viewer: bool) -> None:
    """ALB の入口。ALB のホスト名が決まったら、手持ちの DNS の CNAME（ホスト名 → ALB）が引けるまで待つ。"""
    kapply(manifest=render.alb_ingress_manifest(env, out, viewer), namespaced=False)

    def alb_host() -> str:
        return output(kn("get", "ingress", "litellm", "-o",
                         "jsonpath={.status.loadBalancer.ingress[0].hostname}"), check=False).strip()

    wait_until(600, "ALB の作成", lambda: bool(alb_host()))
    host = alb_host()
    write_if_changed(BUILD_DIR / "aws-alb-hostname.txt", host + "\n")
    info(f"ALB: {host}")
    aws.wait_for_cname(env, host)


def load_audit_image() -> dict:
    if not AUDIT_IMAGE_FILE.exists():
        raise LabError("audit-lab のイメージ名がありません。先に ./lab images を流してください")
    return json.loads(AUDIT_IMAGE_FILE.read_text())


def audit(env: Env) -> None:
    """監査ログの整形の定期処理と、利用料・入出力の履歴を検索するアプリ（変更設計書 5.8 節、6 章）。
    請求の単位と換算のレートが 1 つも無ければ、.env の初期値（LAB_CONTRACTS、LAB_FX_RATES）を書く。"""
    step("audit 監査ログの整形と検索アプリ")
    image = load_audit_image()
    ref = f"{image['repository']}:{image['tag']}"
    if env.is_aws:
        common = {"IMAGE": ref, "S3_ENDPOINT": "", "S3_REGION": env.aws_region,
                  "S3_BUCKET_PREFIX": env.get("LAB_AWS_BUCKET_PREFIX")}
    else:
        common = {"IMAGE": ref, "S3_ENDPOINT": env.get("LAB_S3_ENDPOINT", render.DEFAULT_S3_ENDPOINT),
                  "S3_REGION": env.get("LAB_S3_REGION", "us-east-1"), "S3_BUCKET_PREFIX": ""}
    kapply(manifest=render_template(K8S / "audit-compactor.yaml", common))
    derived = render.bucket_name(env, render.AUDIT_DERIVED)
    with objstore.admin_s3(env) as s3:
        for kind, doc in (("contracts", render.initial_contracts(env)), ("fx_rates", render.initial_fx_rates(env))):
            if not s3.list_objects_v2(Bucket=derived, Prefix=f"billing/{kind}/").get("KeyCount"):
                s3.put_object(Bucket=derived, Key=f"billing/{kind}/00000000T000000-lab-initial.json",
                              Body=json.dumps(doc, ensure_ascii=False, indent=2).encode())
                info(f"{kind} の初期値を書きました")
    if render.audit_oidc_client(env)[0]:
        values = common | {"BASE_URL": env.base_url, "OIDC_ISSUER": env.get("LAB_AUDIT_OIDC_ISSUER",
                                                                              "https://accounts.google.com"),
                           "ADMINS": env.get("LAB_AUDIT_ADMINS", env.get("LAB_USER1_EMAIL")),
                           "AUDITORS": env.get("LAB_AUDITORS"), "ROLE_CLAIM": env.get("LAB_AUDIT_ROLE_CLAIM")}
        values["CONFIG_HASH"] = short_hash(json.dumps(values, sort_keys=True) + json.dumps(
            render.audit_oidc_client(env)) + env.get("AUDIT_SESSION_SECRET"))
        kapply(manifest=render_template(K8S / "audit-viewer.yaml", values))
        rollout("deploy/audit-viewer", 300, "検索アプリの起動")
        ok(f"検索アプリ: {env.base_url}/lab-audit/")
    else:
        if succeeds(kn("get", "deploy", "audit-viewer")):
            run(kn("delete", "deploy/audit-viewer", "service/audit-viewer", "--ignore-not-found"))
            if not env.is_aws:
                run(kn("delete", "ingressroute/audit-viewer", "--ignore-not-found"))
        warn("サインインの設定（Google の OAuth クライアントか LAB_AUDIT_OIDC_*）が無いので、検索アプリは入れません")
    ok("監査ログの段階が終わりました")


def migrate_db(image: dict, db_id: str | None = None) -> None:
    """DB 移行の Job（変更設計書 3.5 節）。名前はイメージと DB（Mac は Cluster の uid、AWS は Aurora の不変の ID）の
    ハッシュ。成功済みなら流さない。DB を作り直すと ID が変わるので移行が流れ直る。"""
    uid = db_id or output(kn("get", "cluster", "litellm-pg", "-o", "jsonpath={.metadata.uid}"), check=False).strip()
    if not uid:
        raise LabError("PostgreSQL（cluster/litellm-pg）がありません。先に ./lab datastores を流してください")
    name = f"litellm-migrate-{short_hash(image['tag'] + uid)}"
    job = json_output(kn("get", "job", name, "-o", "json"), default=None)
    if job and (job.get("status") or {}).get("succeeded"):
        ok(f"DB 移行は済んでいます（{name}）")
    else:
        if job:
            run(kn("delete", "job", name, "--wait=true"))
        info(f"DB 移行の Job {name} を流します")
        kapply(manifest=render_template(K8S / "litellm-migrate.yaml", {
            "JOB_NAME": name, "IMAGE": f"{image['repository']}:{image['tag']}"}))

        def done() -> bool:
            st = (json_output(kn("get", "job", name, "-o", "json"), default={}) or {}).get("status") or {}
            for c in st.get("conditions") or []:
                if c.get("type") == "Failed" and c.get("status") == "True":
                    run(kn("logs", f"job/{name}", "--tail=50"), check=False)
                    raise LabError(f"DB 移行が失敗しました（{name}）")
            return bool(st.get("succeeded"))

        wait_until(300, "DB 移行", done)
    # 古いハッシュの Job を片付ける
    for other in output(kn("get", "job", "-l", "app=litellm-migrate", "-o", "name"), check=False).split():
        if other != f"job.batch/{name}":
            run(kn("delete", other, "--ignore-not-found"), check=False, quiet=True)


def bootstrap(env: Env) -> None:
    """検証ユーザーと、利用者ごとのキー（Claude Code 用、Codex 用）。
    キーの値は .env の固定値を使うので、DB を作り直しても同じキーが作り直される。
    既にあれば作らず、役割と MCP の権限だけを揃える。"""
    step("bootstrap 初期データ")
    if env.is_aws and not env.get("LITELLM_MASTER_KEY"):
        aws.load_app_secret(env)
    # ローリング更新の直後は、終了中の Pod に当たって一時的に失敗することがあるので、少し待つ
    wait_until(60, f"LiteLLM への接続（{env.base_url}）", lambda: http_ok(f"{env.base_url}/health/liveliness"))
    if not env.has_users():
        warn("LAB_USER1_EMAIL が未設定なので、ユーザーとキーは作りません（試験はマスターキーで行えます）")
        return
    perm = {"mcp_servers": ["gmail"] if env.has_google() else []}

    def ensure_user(email: str, role: str) -> None:
        found = admin_api(env, "GET", "/user/info?" + _q(user_id=email)).json() or {}
        if (found.get("user_info") or {}).get("user_id"):
            admin_api(env, "POST", "/user/update", {"user_id": email, "user_role": role})
            info(f"ユーザー {email} は作成済みです（役割 {role}）")
            return
        res = admin_api(env, "POST", "/user/new",
                        {"user_id": email, "user_email": email, "user_role": role, "auto_create_key": False})
        if not (res.json() or {}).get("user_id"):
            raise LabError(f"ユーザー {email} を作れませんでした（{res.status}）: {res.body[:300]}")
        ok(f"ユーザー {email} を作りました（役割 {role}）")

    def ensure_key(email: str, alias: str, key: str, metadata: dict | None = None) -> None:
        body: dict = {"key": key, "object_permission": perm}
        if metadata is not None:
            body["metadata"] = metadata
        found = admin_api(env, "GET", "/key/info?" + _q(key=key)).json() or {}
        info_ = found.get("info") or {}
        note = "、審査なし" if metadata and metadata.get("opted_out_global_guardrails") else ""
        # status が active のときだけ「作成済み」。削除済み（status=deleted）は key/info に別名が残るので作り直す
        if info_.get("status") == "active":
            admin_api(env, "POST", "/key/update", body)
            info(f"キー {alias} は作成済みです（MCP: {perm['mcp_servers']}{note}）")
            return
        res = admin_api(env, "POST", "/key/generate", {**body, "user_id": email, "key_alias": alias})
        if not (res.json() or {}).get("key"):
            raise LabError(f"キー {alias} を作れませんでした（{res.status}）: {res.body[:300]}")
        ok(f"キー {alias} を作りました（MCP: {perm['mcp_servers']}{note}）")

    users = [("1", env.get("LAB_USER1_EMAIL"), "proxy_admin")]
    if env.get("LAB_USER2_EMAIL"):
        users.append(("2", env.get("LAB_USER2_EMAIL"), "internal_user"))
    for n, email, role in users:
        ensure_user(email, role)
        ensure_key(email, f"user{n}-claude", env.get(f"LAB_KEY_USER{n}_CLAUDE"))
        ensure_key(email, f"user{n}-codex", env.get(f"LAB_KEY_USER{n}_CODEX"))
    # 審査を外したキー（ユーザー 1 のみ。LAB_NOREVIEW_KEYS=true のときだけ。docs/manual-check.md）
    nr_aliases = ["user1-claude-noreview", "user1-codex-noreview"]
    if render.noreview_enabled(env):
        u1 = env.get("LAB_USER1_EMAIL")
        nr = {"opted_out_global_guardrails": ["tool-call-review"]}
        ensure_key(u1, "user1-claude-noreview", env.get("LAB_KEY_USER1_CLAUDE_NOREVIEW"), nr)
        ensure_key(u1, "user1-codex-noreview", env.get("LAB_KEY_USER1_CODEX_NOREVIEW"), nr)
    else:
        # 無効に戻したら、審査を通らずに使えるキーを残さない（別名で削除。無ければ何もしない）
        for alias, key in zip(nr_aliases, [env.get("LAB_KEY_USER1_CLAUDE_NOREVIEW"), env.get("LAB_KEY_USER1_CODEX_NOREVIEW")]):
            if not key:
                continue
            found = admin_api(env, "GET", "/key/info?" + _q(key=key)).json() or {}
            if (found.get("info") or {}).get("status") == "active":
                admin_api(env, "POST", "/key/delete", {"keys": [key]})
                info(f"審査なしキー {alias} を削除しました（LAB_NOREVIEW_KEYS=false）")
    ok("初期データの投入が終わりました")


def _q(**params: str) -> str:
    from urllib.parse import urlencode
    return urlencode(params)


def chatgpt_login(env: Env) -> None:
    """chatgpt/ プロバイダのデバイスコードでのログイン（手作業。初回だけ）。
    トークン（auth.json）は LAB_DATA_DIR/chatgpt に保存され、クラスタを作り直しても残る。

    LiteLLM のログイン処理は、URL とコードを表示するだけでブラウザを開かない。ここで URL を拾って開く。
    また、コードを要求した時刻だけが auth.json に残っていると、LiteLLM は 5 分間、新しいコードを出さずに
    黙って待つ（2026-10-11 に確認）。トークンの無い auth.json は消してから始める。"""
    import re
    import subprocess

    step("chatgpt-login ChatGPT のログイン")
    token = env.chatgpt_token_file
    if env.has_chatgpt():
        ok(f"ChatGPT のトークンはあります（{token}）")
        return
    if not sys.stdin.isatty():
        raise LabError("対話の操作が要ります。ターミナルで ./lab chatgpt-login を実行してください")
    image = load_image() if IMAGE_FILE.exists() else images(env)
    token.parent.mkdir(parents=True, exist_ok=True)
    if token.exists():
        token.unlink()  # トークンが入っていない（has_chatgpt が偽）ので、要求の時刻の記録だけ
        info("前回のログインの途中の記録を消しました")
    sys.stderr.write(
        "\nこの後ブラウザで ChatGPT のデバイスコードのページが開きます。ChatGPT Plus のアカウントで\n"
        "サインインし、下に表示されるコード（XXXX-XXXXX の形）を入力してください。\n"
        "（先に ChatGPT の設定 →「セキュリティ」で、Codex などのデバイスコードによるサインインを有効にしておく。\n"
        " 無効のままだと、コードを入力した後に有効にするよう求められて止まります）\n"
        "コードは他人に見せないでください。入力を待つ時間は最大 15 分です。\n\n")
    sys.stderr.flush()
    proc = subprocess.Popen([
        "docker", "run", "--rm", "-i", "-v", f"{token.parent}:/token", "-e", "CHATGPT_TOKEN_DIR=/token",
        "-e", "PYTHONUNBUFFERED=1", "--entrypoint", "python", image["local"], "-c",
        "from litellm.llms.chatgpt.authenticator import Authenticator; Authenticator().get_access_token(); print('OK')",
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    opened = False
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            m = re.search(r"Visit (https://\S+)", line)
            if m and not opened:
                subprocess.run(["open", m.group(1)], check=False)
                opened = True
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise
    if not env.has_chatgpt():
        raise LabError("トークンが保存されませんでした（もう一度 ./lab chatgpt-login を実行できます）")
    token.chmod(0o600)
    ok("ログインしました。./lab up を流すと、Codex 用のモデルと二次判定が有効になります")


def clients(env: Env) -> None:
    """Claude Code と Codex の検証用の設定を生成する。利用者の普段の設定（~/.claude、~/.codex）は書き換えない。
    生成先: clients/out/（Claude Code）、.codex-lab/（Codex の CODEX_HOME）、
    .codex-lab-noreview/（審査を外したキーの Codex の CODEX_HOME）。
    どのユーザーのキーを使うかは .env の LAB_CLIENT_USER（1 か 2、既定 1）で選ぶ。"""
    step("clients クライアントの設定")
    if env.is_aws and not env.get("LITELLM_MASTER_KEY"):
        aws.load_app_secret(env)
    user = env.get("LAB_CLIENT_USER", "1")
    if not env.has_users():
        warn("検証ユーザーが未設定なので、マスターキーで設定を作ります")
    claude_key, codex_key = render.client_keys(env, user)

    out = ROOT / "clients" / "out"
    codex_home = ROOT / ".codex-lab"
    noreview_codex_home = ROOT / ".codex-lab-noreview"
    dirs = [out, codex_home] + ([noreview_codex_home] if render.noreview_enabled(env) else [])
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    dests = {"codex-config.toml": codex_home / "config.toml",
             "noreview-codex-config.toml": noreview_codex_home / "config.toml"}
    for name, content in render.client_files(env, claude_key, codex_key, codex_home, noreview_codex_home).items():
        dest = dests.get(name, out / name)
        if write_if_changed(dest, content, mode=0o600):
            info(f"{dest.relative_to(ROOT)} を更新しました")
    if not render.noreview_enabled(env):
        # 審査なしが無効なら、起動用の設定を残さない
        for p in (out / "noreview-codex.env", out / "noreview-claude.env", out / "noreview-mcp.json",
                  noreview_codex_home / "config.toml"):
            if p.exists():
                p.unlink()
                info(f"{p.relative_to(ROOT)} を削除しました（LAB_NOREVIEW_KEYS=false）")
    ok(f"クライアントの設定ができました（ユーザー {user}）: clients/claude-lab、clients/codex-lab で起動します")
    if not env.has_chatgpt():
        warn("ChatGPT のトークンが無いので、Codex はまだ使えません（./lab chatgpt-login）")
