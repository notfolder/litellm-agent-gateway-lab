"""構築ツールの共通部分: 定数、ログ、.env、外部コマンド、待機、Kubernetes への反映、HTTP。

方針（docs/design.md 17 章）: 状態を見てから動く。何度流しても同じ結果になる。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / ".build"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

CLUSTER = "litellm"
KUBE_CONTEXT = f"k3d-{CLUSTER}"          # Mac（k3d）。AWS は Env.load() が set_kube_context で差し替える
AWS_KUBE_CONTEXT = "litellm-lab-aws"      # aws eks update-kubeconfig --alias で付ける名前
NS = "litellm-lab"
HELM_RELEASE = "litellm"
LITELLM_CHART = "oci://ghcr.io/berriai/litellm-helm"
DEFAULT_LITELLM_VERSION = "v1.104.2"
K3S_IMAGE = "rancher/k3s:v1.35.5-k3s1"

# イメージの置き場（クラスタとは別に作り、クラスタを消しても残す）
REGISTRY_NAME = "litellm-registry.localhost"  # 自作イメージを push する
REGISTRY_HOST_PORT = 5050
CACHE_NAME = "dockerio-cache.localhost"  # Docker Hub の中継キャッシュ
CACHE_HOST_PORT = 5051
REGISTRY_IN_CLUSTER = f"k3d-{REGISTRY_NAME}:5000"  # クラスタの中から見た自作イメージの置き場

CHATGPT_TOKEN_IN_NODE = "/var/lib/lab/chatgpt"


class LabError(Exception):
    """利用者に伝えて止めるエラー（スタックトレースを出さない）。"""


# ---- ログ ----
def _log(color: str, msg: str) -> None:
    sys.stderr.write(f"\033[{color}m[{time.strftime('%H:%M:%S')}] {msg}\033[0m\n")
    sys.stderr.flush()


def info(msg: str) -> None:
    _log("36", msg)


def ok(msg: str) -> None:
    _log("32", msg)


def warn(msg: str) -> None:
    _log("33", f"警告: {msg}")


def step(title: str) -> None:
    sys.stderr.write(f"\n\033[1m== {title} ==\033[0m\n")
    sys.stderr.flush()


# ---- .env ----
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE の行を読む。コメントと空行は飛ばす。値の前後の引用符は外す。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[m.group(1)] = value
    return values


def set_env_line(text: str, key: str, value: str) -> str:
    """KEY の行を書き換える（無ければ末尾に足す）。ほかの行とコメントはそのまま残す。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) == key and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


@dataclass
class Env:
    """.env の内容と、そこから決まる値。"""

    values: dict[str, str]

    @classmethod
    def load(cls) -> "Env":
        if not ENV_FILE.exists():
            raise LabError(".env がありません。./lab up（preflight）で作られます")
        env = cls(parse_env(ENV_FILE.read_text()))
        if env.target not in ("local", "aws"):
            raise LabError(f"LAB_TARGET は local か aws です（{env.target}）")
        # kubectl / helm が向くクラスタを環境に合わせる（変更仕様書 2026-10-24-AWS構成 2.3 節）
        set_kube_context(AWS_KUBE_CONTEXT if env.is_aws else f"k3d-{CLUSTER}")
        # 設定は .env だけから読む。コマンドの前に付けた値は効かないので、食い違えば知らせる
        shell = os.environ.get("LAB_NOREVIEW_KEYS", "").strip()
        if shell and shell.lower() != env.get("LAB_NOREVIEW_KEYS", "false").lower():
            warn(f"シェルの LAB_NOREVIEW_KEYS={shell} は使いません。.env の値"
                 f"（{env.get('LAB_NOREVIEW_KEYS', 'false')}）で動きます。変えるときは .env を書き換えてください")
        return env

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, "").strip() or default

    def fill(self, key: str, value: str) -> bool:
        """.env の KEY が空のときだけ VALUE を書き込む（既存の値は決して上書きしない）。"""
        if self.get(key):
            return False
        self.set(key, value)
        info(f".env に {key} を生成しました")
        return True

    def set(self, key: str, value: str) -> None:
        ENV_FILE.write_text(set_env_line(ENV_FILE.read_text(), key, value))
        ENV_FILE.chmod(0o600)
        self.values[key] = value

    # ---- 決まる値 ----
    @property
    def target(self) -> str:
        """向ける環境: local（Mac の k3d）か aws（変更仕様書 2026-10-24-AWS構成 0.2 節）。"""
        return self.get("LAB_TARGET", "local")

    @property
    def is_aws(self) -> bool:
        return self.target == "aws"

    @property
    def data_dir(self) -> Path:
        return Path(self.get("LAB_DATA_DIR", "/Volumes/SSD/litellm-lab"))

    @property
    def http_port(self) -> int:
        return int(self.get("LAB_HTTP_PORT", "8080"))

    @property
    def base_url(self) -> str:
        """クライアントとこのツールが使う入口。Mac は http://localhost:<port>、AWS は https://<ホスト名>。"""
        if self.is_aws:
            return f"https://{self.aws_hostname}"
        return f"http://localhost:{self.http_port}"

    @property
    def public_host(self) -> str:
        """入口のホスト名（ポート付き）。Claude Code の信頼するドメインの記述に使う。"""
        return self.aws_hostname if self.is_aws else f"localhost:{self.http_port}"

    # ---- AWS（LAB_TARGET=aws のときだけ使う） ----
    @property
    def aws_hostname(self) -> str:
        host = self.get("LAB_AWS_HOSTNAME")
        if not host:
            raise LabError("LAB_TARGET=aws には LAB_AWS_HOSTNAME（HTTPS のホスト名）が要ります")
        return host

    @property
    def aws_region(self) -> str:
        return self.get("LAB_AWS_REGION", "ap-northeast-1")

    @property
    def aws_profile(self) -> str:
        return self.get("LAB_AWS_PROFILE")

    @property
    def aws_prefix(self) -> str:
        """CloudFormation のスタック名、Secrets Manager の名前、ECR のリポジトリの接頭辞。"""
        return self.get("LAB_AWS_STACK_PREFIX", "litellm-lab")

    @property
    def aws_cluster_name(self) -> str:
        return f"{self.aws_prefix}-eks"

    @property
    def litellm_version(self) -> str:
        return self.get("LAB_LITELLM_VERSION", DEFAULT_LITELLM_VERSION)

    @property
    def litellm_base_image(self) -> str:
        return f"ghcr.io/berriai/litellm-database:{self.litellm_version}"

    @property
    def chart_version(self) -> str:
        return self.litellm_version.removeprefix("v")

    @property
    def chatgpt_token_file(self) -> Path:
        return self.data_dir / "chatgpt" / "auth.json"

    # ---- 手作業の準備の状況 ----
    def has_google(self) -> bool:
        return bool(self.get("GOOGLE_OAUTH_CLIENT_ID") and self.get("GOOGLE_OAUTH_CLIENT_SECRET"))

    def has_jev(self) -> bool:
        return bool(self.get("TYPESAFE_API_KEY"))

    def has_users(self) -> bool:
        return bool(self.get("LAB_USER1_EMAIL"))

    def has_chatgpt(self) -> bool:
        return has_chatgpt_token(self.chatgpt_token_file)


def has_chatgpt_token(path: Path) -> bool:
    """トークン（refresh_token か access_token）が入っているか。ログインの途中で止まると、
    要求時刻だけの auth.json が残ることがあるので、ファイルの有無では判定しない。"""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("refresh_token") or data.get("access_token"))


def gen_secret(nbytes: int = 24) -> str:
    return secrets.token_hex(nbytes)


def gen_key() -> str:
    return "sk-" + secrets.token_hex(24)


# ---- ファイル ----
def write_if_changed(dest: Path, content: str, mode: int | None = None) -> bool:
    """内容が変わったときだけ書き換える。変わったら True。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    changed = not dest.exists() or dest.read_text() != content
    if changed:
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(content)
        tmp.replace(dest)
    if mode is not None:
        dest.chmod(mode)
    return changed


def render_template(path: Path, values: dict[str, object]) -> str:
    """${NAME} を置き換える。渡していない名前が残っていればエラーにする。"""
    return Template(path.read_text()).substitute({k: str(v) for k, v in values.items()})


def short_hash(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:12]


# ---- 外部コマンド ----
def run(cmd: list[str], *, check: bool = True, capture: bool = False, quiet: bool = False,
        input: str | None = None, timeout: float | None = None, cwd: Path | None = None,
        env_vars: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """外部コマンドを実行する。capture=True なら標準出力を文字列で返す。
    quiet=True なら出力を捨てる。check=True で失敗したら LabError。cwd と env_vars は CDK や aws に渡す。"""
    kwargs: dict = {"text": True, "input": input, "timeout": timeout, "cwd": cwd, "env": env_vars}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    elif quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    else:
        # ツールのログ（kubectl、helm など）は標準エラーに出し、標準出力を汚さない
        kwargs["stdout"] = sys.stderr
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        raise LabError(f"{cmd[0]} が見つかりません（brew install で入れてください）") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip() if capture else ""
        raise LabError(f"コマンドが失敗しました（終了コード {proc.returncode}）: {' '.join(cmd[:4])} …"
                       + (f"\n{detail}" if detail else ""))
    return proc


def succeeds(cmd: list[str], timeout: float | None = 30) -> bool:
    try:
        return run(cmd, check=False, quiet=True, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def output(cmd: list[str], check: bool = True) -> str:
    return run(cmd, check=check, capture=True).stdout


def json_output(cmd: list[str], default=None):
    proc = run(cmd, check=False, capture=True)
    if proc.returncode != 0:
        return default
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return default


def require_commands(*names: str) -> None:
    for name in names:
        if shutil.which(name) is None:
            raise LabError(f"{name} が見つかりません（brew install で入れてください）")


def wait_until(timeout: int, desc: str, check) -> None:
    """check() が真を返すまで待つ。check は関数か、コマンドの配列。"""
    probe = check if callable(check) else (lambda: succeeds(check))
    start = time.monotonic()
    while not probe():
        if time.monotonic() - start >= timeout:
            raise LabError(f"{desc} を {timeout} 秒待ちましたが、終わりませんでした")
        time.sleep(3)
    ok(f"{desc}（{int(time.monotonic() - start)} 秒）")


# ---- Kubernetes ----
def set_kube_context(name: str) -> None:
    """kubectl / helm が向く context。Env.load() が環境（local / aws）に合わせて呼ぶ。"""
    global KUBE_CONTEXT
    KUBE_CONTEXT = name


def kube_context() -> str:
    return KUBE_CONTEXT


def kc(*args: str) -> list[str]:
    return ["kubectl", "--context", KUBE_CONTEXT, *args]


def kn(*args: str) -> list[str]:
    return ["kubectl", "--context", KUBE_CONTEXT, "-n", NS, *args]


def helm(*args: str) -> list[str]:
    return ["helm", "--kube-context", KUBE_CONTEXT, *args]


def kapply(*, files: list[Path] | None = None, manifest: str | None = None, namespaced: bool = True) -> bool:
    """差分があるときだけ kubectl apply する（apply は変化がなくても configured と出ることがあるため）。
    反映したら True。"""
    base = kn if namespaced else kc
    tmp = None
    paths = list(files or [])
    if manifest is not None:
        fd, name = tempfile.mkstemp(suffix=".yaml", dir=BUILD_DIR)
        os.close(fd)
        tmp = Path(name)
        tmp.write_text(manifest)
        paths.append(tmp)
    args: list[str] = []
    for p in paths:
        args += ["-f", str(p)]
    try:
        rc = run(base("diff", *args), check=False, quiet=True).returncode
        if rc == 0:
            names = output(base("get", *args, "-o", "name"), check=False).split()
            for n in names:
                sys.stderr.write(f"{n} unchanged\n")
            return False
        # rc 1 は差分あり。それ以外は diff 自体の失敗（CRD が未登録など）なので、そのまま apply する
        run(base("apply", *args))
        return True
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def secret_manifest(name: str, data: dict[str, str], type_: str = "Opaque") -> str:
    """Secret のマニフェスト（JSON は YAML として有効）。キーの順を固定して、差分が出ないようにする。
    CloudNativePG の初期化に渡す Secret は type_="kubernetes.io/basic-auth" にする。"""
    return json.dumps({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": NS},
        "type": type_,
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in sorted(data.items())},
    }, indent=2)


def configmap_manifest(name: str, data: dict[str, str]) -> str:
    return json.dumps({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": NS},
        "data": dict(sorted(data.items())),
    }, indent=2, ensure_ascii=False)


@contextlib.contextmanager
def port_forward(target: str, remote_port: int, namespace: str = NS):
    """クラスタの中の Service に、その間だけ Mac からつなぐ（./lab は Mac の上で動くため）。
    with の中では http://127.0.0.1:<空いているポート> を返す。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(kc("-n", namespace, "port-forward", target, f"{port}:{remote_port}"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        start = time.monotonic()
        while True:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if proc.poll() is not None or time.monotonic() - start > 30:
                raise LabError(f"{target} への port-forward を張れませんでした")
            time.sleep(0.3)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def rollout(kind_name: str, timeout: int, desc: str) -> None:
    wait_until(timeout, desc, kn("rollout", "status", kind_name, "--timeout=5s"))


# ---- k3d ----
def cluster_exists() -> bool:
    clusters = json_output(["k3d", "cluster", "list", "-o", "json"], default=[]) or []
    return any(c.get("name") == CLUSTER for c in clusters)


def registry_exists(name: str) -> bool:
    regs = json_output(["k3d", "registry", "list", "-o", "json"], default=[]) or []
    return any(r.get("name") == f"k3d-{name}" for r in regs)


def container_running(name: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False, capture=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


# ---- HTTP ----
@dataclass
class HttpResult:
    status: int
    body: str

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def http(method: str, url: str, *, headers: dict[str, str] | None = None, body=None,
         timeout: float = 30) -> HttpResult:
    """HTTP を呼ぶ。エラーの状態コードでも例外にせず、状態コードと本文を返す（接続できなければ 0）。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = (json.dumps(body) if not isinstance(body, (str, bytes)) else body)
        data = data.encode() if isinstance(data, str) else data
        hdrs.setdefault("content-type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return HttpResult(e.code, e.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError):
        return HttpResult(0, "")


def http_ok(url: str, headers: dict[str, str] | None = None) -> bool:
    return 200 <= http("GET", url, headers=headers, timeout=5).status < 300


def admin_api(env: Env, method: str, path: str, body=None) -> HttpResult:
    """LiteLLM の管理 API をマスターキーで呼ぶ。"""
    return http(method, env.base_url + path, body=body,
                headers={"Authorization": f"Bearer {env.get('LITELLM_MASTER_KEY')}"})
