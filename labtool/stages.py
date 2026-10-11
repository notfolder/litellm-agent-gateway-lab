"""構築の各段階。どの段階も、何度実行しても同じ状態になる（docs/design.md 14 章）。"""

from __future__ import annotations

import json
import shutil
import sys

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

# クラスタに入れる部品の版（変更設計書 3.1 節）
CERT_MANAGER = {"repo": "https://charts.jetstack.io", "chart": "cert-manager", "version": "v1.21.2"}
CNPG = {"repo": "https://cloudnative-pg.github.io/charts", "chart": "cloudnative-pg", "version": "0.29.1"}
BARMAN_PLUGIN_VERSION = "v0.15.1"
BARMAN_PLUGIN_URL = ("https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/"
                     f"{BARMAN_PLUGIN_VERSION}/manifest.yaml")
USER_KEYS = ["LAB_KEY_USER1_CLAUDE", "LAB_KEY_USER1_CODEX", "LAB_KEY_USER2_CLAUDE", "LAB_KEY_USER2_CODEX"]


def preflight() -> Env:
    """前提の確認。何も変更しない（.env が無いときに雛形からコピーすることだけを行う）。"""
    step("preflight 前提の確認")
    require_commands("docker", "k3d", "kubectl", "helm")
    if not succeeds(["docker", "info"]):
        raise LabError("Docker が動いていません。OrbStack を起動してください")

    if not ENV_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        ENV_FILE.chmod(0o600)
        info(".env を .env.example から作りました")
    env = Env.load()

    # 外付け SSD（LAB_DATA_DIR の親）がつながっているか
    if not env.data_dir.parent.is_dir():
        raise LabError(f"LAB_DATA_DIR の親フォルダ {env.data_dir.parent} がありません"
                       "（外付け SSD がつながっているか確かめてください）")
    (env.data_dir / "chatgpt").mkdir(parents=True, exist_ok=True)

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
    ok("前提の確認が終わりました")
    return env


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
    """イメージの置き場 2 つ、k3d クラスタ、Traefik の設定、namespace。"""
    step("cluster クラスタ")
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
    operators()
    ok("クラスタの準備ができました")


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
    _ensure_chart("cert-manager", "cert-manager", CERT_MANAGER, {"crds": {"enabled": True}})
    wait_until(300, "cert-manager の準備", kc("-n", "cert-manager", "wait", "--for=condition=Available",
                                              "deploy", "--all", "--timeout=5s"))
    _ensure_chart("cnpg", "cnpg-system", CNPG, {})
    wait_until(300, "CloudNativePG のオペレーターの準備", kc("-n", "cnpg-system", "wait", "--for=condition=Available",
                                                         "deploy", "-l", "app.kubernetes.io/name=cloudnative-pg",
                                                         "--timeout=5s"))
    manifest = BUILD_DIR / f"barman-cloud-{BARMAN_PLUGIN_VERSION}.yaml"
    if not manifest.exists():
        info(f"Barman Cloud Plugin {BARMAN_PLUGIN_VERSION} のマニフェストを取得します")
        res = http("GET", BARMAN_PLUGIN_URL, timeout=60)
        if res.status != 200:
            raise LabError(f"Barman Cloud Plugin のマニフェストを取得できませんでした（{res.status}）")
        write_if_changed(manifest, res.body)
    kapply(files=[manifest], namespaced=False)
    wait_until(300, "Barman Cloud Plugin の準備", kc("-n", "cnpg-system", "rollout", "status", "deploy/barman-cloud",
                                                  "--timeout=5s"))


def images(env: Env) -> dict:
    """検証用 LiteLLM イメージのビルドと push。
    - 中身（Dockerfile、ガードレール、ベースイメージ）のハッシュをタグにする。同じタグがあればビルドしない
    - 自作イメージの置き場に同じタグがあれば push しない
    外部のイメージ（PostgreSQL など）は、ノードが Docker Hub の中継キャッシュ経由で取るので扱わない。"""
    step("images イメージ")
    tag = render.image_tag(env.litellm_base_image, ROOT / "litellm")
    local_image = f"litellm-lab:{tag}"
    push_image = f"localhost:{REGISTRY_HOST_PORT}/litellm-lab:{tag}"
    # クラスタの中から見たイメージ名（Helm に渡す）
    image = {"repository": f"{REGISTRY_IN_CLUSTER}/litellm-lab", "tag": tag, "local": local_image}
    write_if_changed(IMAGE_FILE, json.dumps(image, indent=2) + "\n")

    if succeeds(["docker", "image", "inspect", local_image]):
        ok(f"LiteLLM イメージ {local_image} はビルド済みです")
    else:
        info(f"LiteLLM イメージ {local_image} をビルドします（単体テストも流れます）")
        run(["docker", "build", "--build-arg", f"LITELLM_BASE_IMAGE={env.litellm_base_image}",
             "-t", local_image, str(ROOT / "litellm")])

    accept = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, "
              "application/vnd.oci.image.manifest.v1+json")
    res = http("HEAD", f"http://localhost:{REGISTRY_HOST_PORT}/v2/litellm-lab/manifests/{tag}",
               headers={"Accept": accept}, timeout=10)
    if res.status == 200:
        ok(f"置き場に {tag} があります")
    else:
        info("置き場に push します")
        run(["docker", "tag", local_image, push_image])
        # Mac 側には arm64 の中身しか無いので、arm64 だけを push する
        run(["docker", "push", "--platform", "linux/arm64", "-q", push_image], quiet=True)
        ok(f"push しました: {push_image}")
    return image


def load_image() -> dict:
    if not IMAGE_FILE.exists():
        raise LabError("イメージ名がありません。先に ./lab images を流してください")
    return json.loads(IMAGE_FILE.read_text())


def secrets_(env: Env) -> Env:
    """.env の空の値を生成し、k8s の Secret に反映する。既存の値は上書きしない。"""
    step("secrets 秘密情報")
    env.fill("LITELLM_MASTER_KEY", gen_key())
    env.fill("LITELLM_SALT_KEY", gen_key())
    env.fill("POSTGRES_PASSWORD", gen_secret(16))
    env.fill("REDIS_PASSWORD", gen_secret(16))
    env.fill("SENTINEL_PASSWORD", gen_secret(16))
    for k in USER_KEYS:
        env.fill(k, gen_key())
    # オブジェクトストレージの管理者と、用途ごとの利用者（アクセスキーは利用者の名前、シークレットは生成）
    env.fill("RUSTFS_ACCESS_KEY", "lab-admin")
    env.fill("RUSTFS_SECRET_KEY", gen_secret(20))
    for user in render.S3_USERS:
        key_name, secret_name = render.s3_env_names(user)
        env.fill(key_name, user)
        env.fill(secret_name, gen_secret(20))

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


def objectstore(env: Env) -> None:
    """S3 互換のオブジェクトストレージ（RustFS）と、バケット、利用者、権限（変更設計書 2 章）。
    最後に、許した操作ができ、許していない操作が拒否されることを確かめる。"""
    step("objectstore オブジェクトストレージ")
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

    config_text = render.to_yaml(render.litellm_config(env, env.has_chatgpt()), "./lab が生成。手で編集しない")
    if write_if_changed(BUILD_DIR / "litellm-config.yaml", config_text):
        info("プロキシ設定を更新しました")
    kapply(manifest=configmap_manifest("litellm-config", {"config.yaml": config_text}))

    overrides = render.helm_overrides(env, image["repository"], image["tag"], short_hash(config_text))
    overrides_file = BUILD_DIR / "litellm-overrides.json"
    write_if_changed(overrides_file, json.dumps(overrides, indent=2) + "\n")
    values_file = ROOT / "litellm" / "values.yaml"
    values_hash = short_hash(values_file.read_text() + overrides_file.read_text() + env.chart_version)

    migrate_db(image)
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
    kapply(files=[K8S / "ingress.yaml"])
    wait_until(120, f"入口からの応答（{env.base_url}/health/liveliness）",
               lambda: http_ok(f"{env.base_url}/health/liveliness"))
    ok(f"LiteLLM の準備ができました: {env.base_url}（管理画面 {env.base_url}/ui）")


def migrate_db(image: dict) -> None:
    """DB 移行の Job（変更設計書 3.5 節）。名前はイメージと DB（Cluster の uid）のハッシュ。成功済みなら流さない。"""
    uid = output(kn("get", "cluster", "litellm-pg", "-o", "jsonpath={.metadata.uid}"), check=False).strip()
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

    def ensure_key(email: str, alias: str, key: str) -> None:
        found = admin_api(env, "GET", "/key/info?" + _q(key=key)).json() or {}
        info_ = found.get("info") or {}
        if info_.get("key_alias") or info_.get("token"):
            admin_api(env, "POST", "/key/update", {"key": key, "object_permission": perm})
            info(f"キー {alias} は作成済みです（MCP: {perm['mcp_servers']}）")
            return
        res = admin_api(env, "POST", "/key/generate",
                        {"key": key, "user_id": email, "key_alias": alias, "object_permission": perm})
        if not (res.json() or {}).get("key"):
            raise LabError(f"キー {alias} を作れませんでした（{res.status}）: {res.body[:300]}")
        ok(f"キー {alias} を作りました（MCP: {perm['mcp_servers']}）")

    users = [("1", env.get("LAB_USER1_EMAIL"), "proxy_admin")]
    if env.get("LAB_USER2_EMAIL"):
        users.append(("2", env.get("LAB_USER2_EMAIL"), "internal_user"))
    for n, email, role in users:
        ensure_user(email, role)
        ensure_key(email, f"user{n}-claude", env.get(f"LAB_KEY_USER{n}_CLAUDE"))
        ensure_key(email, f"user{n}-codex", env.get(f"LAB_KEY_USER{n}_CODEX"))
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
    生成先: clients/out/（Claude Code）、.codex-lab/（Codex の CODEX_HOME）。
    どのユーザーのキーを使うかは .env の LAB_CLIENT_USER（1 か 2、既定 1）で選ぶ。"""
    step("clients クライアントの設定")
    user = env.get("LAB_CLIENT_USER", "1")
    if not env.has_users():
        warn("検証ユーザーが未設定なので、マスターキーで設定を作ります")
    claude_key, codex_key = render.client_keys(env, user)

    out = ROOT / "clients" / "out"
    codex_home = ROOT / ".codex-lab"
    for d in (out, codex_home):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    for name, content in render.client_files(env, claude_key, codex_key, codex_home).items():
        dest = codex_home / "config.toml" if name == "codex-config.toml" else out / name
        if write_if_changed(dest, content, mode=0o600):
            info(f"{dest.relative_to(ROOT)} を更新しました")
    ok(f"クライアントの設定ができました（ユーザー {user}）: clients/claude-lab、clients/codex-lab で起動します")
    if not env.has_chatgpt():
        warn("ChatGPT のトークンが無いので、Codex はまだ使えません（./lab chatgpt-login）")
