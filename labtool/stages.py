"""構築の各段階。どの段階も、何度実行しても同じ状態になる（docs/design.md 14 章）。"""

from __future__ import annotations

import json
import shutil
import sys

from . import render
from .core import (
    BUILD_DIR, CACHE_HOST_PORT, CACHE_NAME, CLUSTER, ENV_EXAMPLE, ENV_FILE, HELM_RELEASE, K3S_IMAGE,
    LITELLM_CHART, NS, REGISTRY_HOST_PORT, REGISTRY_IN_CLUSTER, REGISTRY_NAME, ROOT, Env, LabError,
    admin_api, cluster_exists, configmap_manifest, container_running, gen_key, gen_secret, helm, http,
    http_ok, info, json_output, kapply, kc, kn, ok, output, registry_exists, render_template,
    require_commands, rollout, run, secret_manifest, short_hash, step, succeeds, wait_until, warn,
    write_if_changed,
)

K8S = ROOT / "k8s"
IMAGE_FILE = BUILD_DIR / "litellm-image.json"
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
    ok("クラスタの準備ができました")


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
    for k in USER_KEYS:
        env.fill(k, gen_key())

    kapply(manifest=secret_manifest("postgres", {"username": "litellm", "password": env.get("POSTGRES_PASSWORD")}))
    kapply(manifest=secret_manifest("redis", {"password": env.get("REDIS_PASSWORD")}))
    kapply(manifest=secret_manifest("litellm-secrets", render.secrets_for_litellm(env)))
    if env.has_google():
        kapply(manifest=secret_manifest("workspace-mcp", {
            "GOOGLE_OAUTH_CLIENT_ID": env.get("GOOGLE_OAUTH_CLIENT_ID"),
            "GOOGLE_OAUTH_CLIENT_SECRET": env.get("GOOGLE_OAUTH_CLIENT_SECRET"),
        }))
    ok("Secret を反映しました")
    return env


def datastores(env: Env) -> None:
    """PostgreSQL と Redis。差分だけ反映し、Ready を待つ。"""
    step("datastores PostgreSQL と Redis")
    kapply(files=[K8S / "postgres.yaml", K8S / "redis.yaml"])
    rollout("statefulset/postgres", 300, "PostgreSQL の起動")
    rollout("deploy/redis", 180, "Redis の起動")
    ok("PostgreSQL と Redis の準備ができました")


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

    _recover_helm_release()
    current = (json_output(helm("-n", NS, "get", "values", HELM_RELEASE, "-o", "json"), default={}) or {})
    if current.get("labValuesHash") == values_hash and _helm_status() == "deployed":
        ok(f"LiteLLM のリリースは最新です（値のハッシュ {values_hash}）")
    else:
        info("LiteLLM を helm upgrade --install します（DB 移行の Job が先に流れます）")
        run(helm("upgrade", "--install", HELM_RELEASE, LITELLM_CHART, "--version", env.chart_version,
                 "-n", NS, "-f", str(values_file), "-f", str(overrides_file),
                 "--set-string", f"labValuesHash={values_hash}", "--wait", "--timeout", "15m"))

    rollout("deploy/litellm", 600, "LiteLLM の起動")
    kapply(files=[K8S / "ingress.yaml"])
    wait_until(120, f"入口からの応答（{env.base_url}/health/liveliness）",
               lambda: http_ok(f"{env.base_url}/health/liveliness"))
    ok(f"LiteLLM の準備ができました: {env.base_url}（管理画面 {env.base_url}/ui）")


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
