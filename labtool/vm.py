"""VM 共通層（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-30-既存資源からの構築と出口プロキシ/ 3 章・5 章・6 章）。

クラウドを知らない部分: .env の識別子から基盤を組む（load_infra）、SSH（鍵、コマンドの実行、kubeconfig、6443 のポート転送）、
VM の初期化スクリプトの描画と SSH での実行、k3s のトークンと版（バケットの cluster/ ディレクトリ）、VM 上でのイメージのビルド、
中継（出口プロキシ）の VM の初期化、k3s の版上げの部品、証明書の退避、A レコード、アンインストール。
クラウドの API は labtool/clouds/ のモジュール（clouds.for_env(env)）を通してだけ呼ぶ。
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import yaml

from . import clouds, render
from . import objectstore as objstore
from .clouds import PRIORITIES, PROXY_PORT, REGISTRY_HOST, REGISTRY_NODEPORT, TOKEN_KEY, VERSION_KEY, Infra, Node
from .core import (
    BUILD_DIR, K3S_IMAGE, NS, ROOT, VM_KUBE_CONTEXTS, Env, LabError, gen_secret, info, kc, kn, ok, output, run,
    succeeds, wait_until, warn, write_if_changed,
)

KNOWN_HOSTS = BUILD_DIR / "vm-known-hosts"
FORWARD_PID_FILE = BUILD_DIR / "vm-forward.pid"
FORWARD_TARGET_FILE = BUILD_DIR / "vm-forward.target"   # 転送先の Node.id（版上げでその台を避けるため）
FORWARD_LOG = BUILD_DIR / "vm-forward.log"
INFRA_FILE = BUILD_DIR / "vm-infra.json"                 # 最後に組んだ基盤（目安。正は .env と API）
TLS_BACKUP = BUILD_DIR / "vm-tls-secret.json"
EGRESS_CA_FILE = BUILD_DIR / "egress-ca.pem"             # 中継の VM から取った CA（managed）
TLS_SECRET = "litellm-tls"
NODE_INIT_SCRIPT = ROOT / "vm" / "node-init.sh"
PROXY_INIT_SCRIPT = ROOT / "vm" / "proxy-init.sh"
K3S_INSTALL_URL = "https://get.k3s.io"
IP_LOOKUPS = ["https://checkip.amazonaws.com", "https://api.ipify.org"]
UNRETAINED_PREFIXES = [f"{s}/" for s in render.VM_STORES if s not in render.RETAINED_STORES]
# .env に provision が書く項目（managed の down で消す）
PROVISIONED_ENV_KEYS = ["LAB_VM_NODES", "LAB_VM_BUCKET", "LAB_VM_ENTRY_IP_ID", "LAB_VM_PROXY_NODE", "LAB_EGRESS_PROXY",
                        "LAB_EGRESS_PROXY_MAC", "LAB_EGRESS_CA_FILE", render.GCS_REGISTRY_KEY_ENV]


def kube_context(env: Env) -> str:
    return VM_KUBE_CONTEXTS[env.target]


# ---- k3s の版（純粋な関数。単体テストの対象） ----
def k3s_install_version(image: str = K3S_IMAGE) -> str:
    """Mac の k3s のイメージのタグ（v1.35.5-k3s1）を、インストールスクリプトの版の形（v1.35.5+k3s1）にする。"""
    return image.rsplit(":", 1)[1].replace("-k3s", "+k3s")


def parse_k3s_version(text: str) -> tuple[int, int, int]:
    """v1.35.5+k3s1（または v1.35.5-k3s1、kubelet が報告する v1.35.5+k3s1）→ (1, 35, 5)。"""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)[+-]k3s\d+", text.strip())
    if not m:
        raise LabError(f"k3s の版の形が違います: {text!r}（例 v1.36.1+k3s1）")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def normalize_k3s_version(text: str) -> str:
    parse_k3s_version(text)
    return text.strip().replace("-k3s", "+k3s")


def check_upgrade_step(current: str, target: str) -> None:
    """Kubernetes の版の差の規則: マイナー版は 1 つずつ、下げない（同じマイナー版の中のパッチの更新は可）。"""
    cur, tgt = parse_k3s_version(current), parse_k3s_version(target)
    if tgt[:2] < cur[:2] or (tgt[:2] == cur[:2] and tgt[2] < cur[2]):
        raise LabError(f"{target} は今の {current} より古い版です（下げる操作は行いません）")
    if tgt[0] != cur[0] or tgt[1] > cur[1] + 1:
        raise LabError(f"{current} → {target} はマイナー版を飛ばしています。1 つずつ上げてください（例 v{cur[0]}.{cur[1] + 1}.x+k3s1）")


def upgrade_order(node_ids: list[str], entry_holder: str | None, forward_target: str | None = None) -> list[str]:
    """上げる順。浮動 IP を持つ台は最後（入口の移動を 1 回にする）。転送先の台はその前（転送の張り直しを 1 回にする）。"""
    rest = [i for i in node_ids if i not in (entry_holder, forward_target)]
    tail = [i for i in (forward_target, entry_holder) if i and i in node_ids]
    return rest + list(dict.fromkeys(tail))


# ---- 操作者の IP と SSH ----
def operator_cidr(env: Env, lookup=None) -> str:
    """22 を開ける送信元。.env の LAB_SSH_ALLOW_CIDR があればそれ、無ければパブリック IPv4 を外部に問い合わせて /32
    （中継を通さずに問い合わせる。通すと中継の IP が返る）。"""
    fixed = env.get("LAB_SSH_ALLOW_CIDR", "").strip()
    if fixed:
        return fixed if "/" in fixed else f"{fixed}/32"
    lookup = lookup or _lookup_public_ip
    text = (lookup() or "").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text or ""):
        raise LabError(f"操作者のパブリック IPv4 が分かりません（{text!r}）。.env の LAB_SSH_ALLOW_CIDR に書いてください")
    return f"{text}/32"


def web_cidr(env: Env) -> str:
    """80/443 を開ける送信元。.env の LAB_WEB_ALLOW_CIDR があればそれ、無ければ 22 と同じ（操作者の IP）。
    公開の入口には API キーの総当たりが来るので、検証環境は操作者の回線だけに開く（ユーザーの判断 2026-10-30: A）。"""
    fixed = env.get("LAB_WEB_ALLOW_CIDR", "").strip()
    if fixed:
        return fixed if "/" in fixed else f"{fixed}/32"
    return operator_cidr(env)


def _lookup_public_ip() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 中継を通さない
    for url in IP_LOOKUPS:
        try:
            with opener.open(url, timeout=5) as res:
                return res.read().decode().strip()
        except Exception:  # noqa: BLE001 次の問い合わせ先へ
            continue
    return ""


def ensure_ssh_key(env: Env) -> str:
    """SSH の鍵（LAB_VM_SSH_KEY。既定 .build/vm-ssh-key。managed で無ければ作る）。公開鍵の文字列を返す。"""
    key = env.vm_ssh_key
    if not key.exists():
        if env.is_provided:
            raise LabError(f"SSH の秘密鍵 {key}（LAB_VM_SSH_KEY）がありません。用意する側から受け取った鍵のパスを書いてください")
        key.parent.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "litellm-lab", "-f", str(key)], quiet=True)
        info(f"SSH の鍵を {key} に作りました")
    pub = key.with_suffix(".pub")
    if pub.exists():
        return pub.read_text().strip()
    return output(["ssh-keygen", "-y", "-f", str(key)]).strip()


def ssh_base(env: Env, node: Node, *extra: str) -> list[str]:
    if not node.public_ip:
        raise LabError(f"ノード {node.id} に公開 IP がありません（起動を待つか、外向きの IP を確かめてください）")
    # known_hosts は IP でなく台の識別子で引く（GCP は浮動 IP を取った台の公開 IP が静的 IP に入れ替わり、障害で別の台に移る）
    return ["ssh", "-i", str(env.vm_ssh_key), "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"HostKeyAlias={host_alias(node)}", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
            *extra, f"{env.vm_ssh_user}@{node.public_ip}"]


def host_alias(node: Node) -> str:
    """known_hosts の鍵にする台の識別子（GCP の <ゾーン>/<名前> の / は _ に）。"""
    return node.id.replace("/", "_")


def run_on(env: Env, node: Node, script: str, timeout: int = 120, input_bytes: bytes | None = None) -> str:
    """VM の中でシェルのスクリプトを root で流し、標準出力を返す。input_bytes があれば、スクリプトでなくそれを標準入力にして
    script をコマンドとして流す（イメージのビルドの文脈の tar など）。"""
    if input_bytes is None:
        proc = run([*ssh_base(env, node), "sudo", "bash", "-s"], input=script, capture=True, check=False, timeout=timeout + 30)
    else:
        proc = subprocess.run([*ssh_base(env, node), "sudo", "bash", "-c", script], input=input_bytes,
                              capture_output=True, timeout=timeout + 30)
        proc.stdout, proc.stderr = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise LabError(f"{node.id} でのコマンドが失敗しました（終了コード {proc.returncode}）: {(proc.stderr or proc.stdout or '')[-1500:]}")
    return proc.stdout


def forget_host(node: Node) -> None:
    """台を作り直したとき、known_hosts からその台の行を消す。"""
    if KNOWN_HOSTS.exists():
        lines = [line for line in KNOWN_HOSTS.read_text().splitlines() if not line.startswith(host_alias(node) + " ")]
        KNOWN_HOSTS.write_text("\n".join(lines) + ("\n" if lines else ""))


def _port_open(ip: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(3)
        return s.connect_ex((ip, port)) == 0


# ---- 基盤（.env の識別子から組む） ----
def save_infra(infra: Infra) -> None:
    write_if_changed(INFRA_FILE, json.dumps(asdict(infra), indent=2, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)


def require_provided_env(env: Env) -> None:
    """既存資源の .env の項目がそろっているか（provided。managed は provision が書く）。"""
    missing = []
    if len(env.vm_nodes) != clouds.NODE_COUNT:
        missing.append(f"LAB_VM_NODES（VM の識別子 {clouds.NODE_COUNT} つをカンマ区切り。今 {len(env.vm_nodes)} つ）")
    for key, what in (("LAB_VM_BUCKET", "バケット名"), ("LAB_VM_ENTRY_IP_ID", "浮動 IP の識別子")):
        if not env.get(key):
            missing.append(f"{key}（{what}）")
    users = [*clouds.STATIC_KEY_USERS] + ([clouds.OPERATOR_USER] if env.is_gcp_vm else [])
    for user in users:
        if user == "registry" and env.is_gcp_vm:
            if not env.get(render.GCS_REGISTRY_KEY_ENV):
                missing.append(f"{render.GCS_REGISTRY_KEY_ENV}（registry のサービスアカウントの JSON キーを base64 で。GCS ドライバ用）")
            continue
        key_name, secret_name = render.s3_env_names(user, env)
        if not (env.get(key_name) and env.get(secret_name)):
            missing.append(f"{key_name} / {secret_name}（{user} の静的キー）")
    if env.egress_proxy and (env.egress_ca_file is None or not env.egress_ca_file.exists()):
        missing.append("LAB_EGRESS_CA_FILE（中継の CA の PEM。LAB_EGRESS_PROXY があるとき必須）")
    if missing:
        raise LabError("LAB_INFRA=provided には .env の次の項目が要ります（用意する側から受け取る。変更設計書 2 章）:\n  "
                       + "\n  ".join(missing))


def load_infra(env: Env) -> Infra:
    """基盤を .env の識別子と API で組む。無ければエラー。"""
    if len(env.vm_nodes) != clouds.NODE_COUNT or not env.vm_bucket:
        raise LabError(f"{env.target} の基盤の識別子（LAB_VM_NODES、LAB_VM_BUCKET）が .env にありません。"
                       "managed なら ./lab cluster が作って書き、provided なら用意する側から受け取って書いてください")
    cloud = clouds.for_env(env)
    infra = Infra(region=env.vm_region, bucket=env.vm_bucket, entry_ip_id=env.vm_entry_ip_id)
    infra.nodes = cloud.describe_nodes(env, env.vm_nodes)
    if env.vm_entry_ip_id:
        infra.entry_ip = cloud.describe_entry_ip(env, env.vm_entry_ip_id)
    proxy_id = env.get("LAB_VM_PROXY_NODE")
    if proxy_id:
        infra.proxy = cloud.describe_nodes(env, [proxy_id])[0]
        infra.proxy.index = 0
    save_infra(infra)
    return infra


def try_load_infra(env: Env) -> Infra | None:
    try:
        return load_infra(env)
    except LabError:
        return None


def ensure_access(env: Env) -> None:
    """VM に届く状態にする: managed は 22 と 80/443（と中継）の許可を操作者の今の IP に合わせ、ポート転送を確かめる。
    provided はネットワークを触らず、転送だけ。基盤が無ければ何もしない。"""
    infra = try_load_infra(env)
    if infra is None or not infra.nodes:
        return
    if not env.is_provided:
        cloud = clouds.for_env(env)
        cidr = operator_cidr(env)
        cloud.allow_ssh_from(env, cidr)
        cloud.allow_web_from(env, web_cidr(env))
        if env.egress_proxy_vm:
            cloud.allow_proxy_from(env, cidr)
    ensure_forward(env, infra)


def refresh_public_ips(env: Env, infra: Infra) -> bool:
    """台の公開 IP をクラウドから引き直す（GCP は浮動 IP を取った台の公開 IP が静的 IP に変わる。配備で判明 2026-10-30）。"""
    fresh = {n.index: n for n in clouds.for_env(env).describe_nodes(env, env.vm_nodes)}
    changed = False
    for n in infra.nodes:
        f = fresh.get(n.index)
        if f and f.public_ip and f.public_ip != n.public_ip:
            info(f"{n.id} の公開 IP が {n.public_ip or '(無し)'} → {f.public_ip} に変わりました")
            n.public_ip = f.public_ip
            changed = True
    if changed:
        save_infra(infra)
    return changed


def wait_ssh(env: Env, nodes: list[Node], infra: Infra | None = None, timeout: int = 600) -> None:
    def reachable() -> bool:
        if all(n.public_ip and _port_open(n.public_ip, 22) for n in nodes):
            return True
        if infra is not None:
            refresh_public_ips(env, infra)
        return False
    wait_until(timeout, f"{len(nodes)} 台の SSH（22）", reachable)


# ---- kubeconfig とポート転送 ----
def fetch_kubeconfig(env: Env, node: Node) -> None:
    """k3s の kubeconfig を 1 台から読み、ポート転送の宛先（127.0.0.1）に向けたコンテキストを ~/.kube/config に書く。"""
    cfg = yaml.safe_load(run_on(env, node, "cat /etc/rancher/k3s/k3s.yaml", timeout=30))
    cluster = cfg["clusters"][0]["cluster"]
    user = cfg["users"][0]["user"]
    ctx = kube_context(env)
    with tempfile.TemporaryDirectory() as d:
        files = {}
        for name, value in (("ca", cluster["certificate-authority-data"]), ("cert", user["client-certificate-data"]),
                            ("key", user["client-key-data"])):
            p = Path(d) / name
            p.write_bytes(base64.b64decode(value))
            files[name] = str(p)
        run(["kubectl", "config", "set-cluster", ctx, f"--server=https://127.0.0.1:{env.vm_local_port}",
             f"--certificate-authority={files['ca']}", "--embed-certs=true"], quiet=True)
        run(["kubectl", "config", "set-credentials", ctx, f"--client-certificate={files['cert']}",
             f"--client-key={files['key']}", "--embed-certs=true"], quiet=True)
        run(["kubectl", "config", "set-context", ctx, f"--cluster={ctx}", f"--user={ctx}"], quiet=True)
    ok(f"kubectl の context {ctx} を更新しました（{node.id} から）")


def forward_alive() -> bool:
    return succeeds(kc("get", "--raw", "/readyz", "--request-timeout=5s"), timeout=15)


def forward_target() -> str | None:
    return FORWARD_TARGET_FILE.read_text().strip() or None if FORWARD_TARGET_FILE.exists() else None


def ensure_forward(env: Env, infra: Infra, avoid: str | None = None) -> None:
    """k3s の API への SSH のポート転送を確かめ、無ければ張る。転送は背景に残し（./lab を何度も流すため）、./lab down で止める。
    avoid の台（版上げで k3s を再起動する台）に張ってあれば、別の台へ張り直す。"""
    if forward_alive() and (avoid is None or forward_target() != avoid):
        return
    refresh_public_ips(env, infra)
    nodes = [n for n in infra.nodes if n.id != avoid and n.public_ip and _port_open(n.public_ip, 22)]
    if not nodes:
        raise LabError("SSH で届くノードがありません（./lab cluster で作るか、起動を待ってください）")
    stop_forward()
    fetch_kubeconfig(env, nodes[0])
    port = env.vm_local_port
    if _port_open("127.0.0.1", port):
        raise LabError(f"ローカルのポート {port} が別のプロセスに使われています（.env の LAB_VM_LOCAL_PORT を変えてください）")
    FORWARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(FORWARD_LOG, "ab")
    proc = subprocess.Popen(
        ssh_base(env, nodes[0], "-N", "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
                 "-L", f"{port}:127.0.0.1:6443"),
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    FORWARD_PID_FILE.write_text(str(proc.pid))
    FORWARD_TARGET_FILE.write_text(nodes[0].id)
    info(f"SSH のポート転送を張ります（{nodes[0].id} の 6443 → 127.0.0.1:{port}）")
    wait_until(90, "k3s の API への接続", lambda: proc.poll() is None and forward_alive())


def stop_forward() -> None:
    FORWARD_TARGET_FILE.unlink(missing_ok=True)
    if not FORWARD_PID_FILE.exists():
        return
    try:
        os.killpg(int(FORWARD_PID_FILE.read_text().strip()), signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    FORWARD_PID_FILE.unlink(missing_ok=True)


@contextlib.contextmanager
def registry_forward():
    """クラスタの中のイメージ置き場（registry）に Mac からつなぐ。with の中では http://127.0.0.1:<ポート>。"""
    from .core import port_forward
    with port_forward(f"svc/{render.VM_REGISTRY_NAME}", 5000) as endpoint:
        yield endpoint


# ---- k3s のトークンと版（バケットの cluster/。操作者が読み書きし、VM には SSH で渡す） ----
def ensure_cluster_objects(env: Env) -> tuple[str, str]:
    """cluster/k3s/token（無ければ乱数）と cluster/k3s/version（無ければコードの版）。配備済みの版とコードの版が
    違えば止める（上げるのは ./lab upgrade-k8s だけ。./lab up では版を変えない）。(トークン, 版) を返す。"""
    from botocore.exceptions import ClientError
    bucket = env.vm_bucket
    with objstore.admin_s3(env) as s3:
        def get(key: str) -> str | None:
            try:
                return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode().strip()
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                    return None
                raise
        token = get(TOKEN_KEY)
        if token is None:
            token = gen_secret(32)
            s3.put_object(Bucket=bucket, Key=TOKEN_KEY, Body=token.encode())
            info(f"k3s の参加トークンを {bucket}/{TOKEN_KEY} に置きました")
        version = get(VERSION_KEY)
        if version is None:
            version = k3s_install_version()
            s3.put_object(Bucket=bucket, Key=VERSION_KEY, Body=version.encode())
            info(f"k3s の版 {version} を {bucket}/{VERSION_KEY} に置きました")
        elif version != k3s_install_version():
            raise LabError(f"配備済みの k3s の版（{version}）とコードの版（{k3s_install_version()}）が違います。"
                           f"上げるなら ./lab upgrade-k8s {k3s_install_version()}、戻すなら core.py の K3S_IMAGE を "
                           f"{version} に合わせてください（./lab up では版を変えません）")
    return token, version


def set_version(env: Env, version: str) -> None:
    with objstore.admin_s3(env) as s3:
        s3.put_object(Bucket=env.vm_bucket, Key=VERSION_KEY, Body=version.encode())


# ---- VM の初期化（SSH） ----
def egress_values(env: Env, infra: Infra) -> dict[str, str]:
    """初期化スクリプトに渡す中継の値（URL、除外、CA の base64）。中継が無ければ空。"""
    if not env.egress_proxy:
        return {"EGRESS_PROXY": "", "EGRESS_NO_PROXY": "", "EGRESS_CA_B64": ""}
    no_proxy = render.NODE_NO_PROXY_BASE + "," + env.vm_cidr + "," + ",".join(n.private_ip for n in infra.nodes if n.private_ip)
    ca = render.egress_ca_text(env)
    return {"EGRESS_PROXY": env.egress_proxy, "EGRESS_NO_PROXY": no_proxy,
            "EGRESS_CA_B64": base64.b64encode(ca.encode()).decode() if ca else ""}


def render_node_init(node: Node, all_nodes: list[Node], hooks: str, pubkey: str, values: dict[str, str] | None = None,
                     script: str | None = None) -> str:
    """vm/node-init.sh の @@名前@@ をこの台の値で置き換え、クラウド固有の hooks を差し込む。
    values: K3S_VERSION、K3S_TOKEN、EGRESS_PROXY、EGRESS_NO_PROXY、EGRESS_CA_B64、SSH_USER。"""
    text = script if script is not None else NODE_INIT_SCRIPT.read_text()
    ordered = sorted(all_nodes, key=lambda n: n.index)
    peers = [n.private_ip for n in ordered if n.index != node.index]
    filled = {
        "NODE_INDEX": str(node.index), "SELF_IP": node.private_ip, "FIRST_IP": ordered[0].private_ip,
        "PEER_IPS": " ".join(peers), "PRIORITY": str(PRIORITIES[node.index - 1]), "REGISTRY_HOST": REGISTRY_HOST,
        "REGISTRY_NODEPORT": str(REGISTRY_NODEPORT), "SSH_PUBKEY": pubkey, "HOOKS": hooks,
        "K3S_VERSION": "", "K3S_TOKEN": "", "EGRESS_PROXY": "", "EGRESS_NO_PROXY": "", "EGRESS_CA_B64": "", "SSH_USER": "ubuntu",
    }
    filled.update(values or {})
    for k, v in filled.items():
        text = text.replace(f"@@{k}@@", v)
    left = re.findall(r"@@[A-Z_0-9]+@@", text)
    if left:
        raise ValueError(f"node-init.sh に置き換えていない名前が残っています: {left}")
    return text


def init_nodes(env: Env, infra: Infra, pubkey: str) -> None:
    """3 台を 1 番から順に SSH で初期化する（1 番がクラスタを起こし、2・3 番が参加する）。2 回目以降は中継と CA の設定だけ直す。
    中継を通さないか、まだ中継の VM が無い間は、外へ出る手順（apt、k3s）が失敗するので、中継は先に初期化しておく。"""
    cloud = clouds.for_env(env)
    token, version = ensure_cluster_objects(env)
    wait_ssh(env, infra.nodes, infra)
    common = {"K3S_VERSION": version, "K3S_TOKEN": token, "SSH_USER": env.vm_ssh_user, **egress_values(env, infra)}
    for node in sorted(infra.nodes, key=lambda n: n.index):
        script = render_node_init(node, infra.nodes, cloud.node_init_hooks(env, infra, node), pubkey, common)
        info(f"{node.id}（node{node.index}）を初期化します（SSH）")
        out = run_on(env, node, script, timeout=1500)
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        ok(f"{node.id}: {tail}")
        refresh_public_ips(env, infra)   # GCP は 1 番が浮動 IP を取ると公開 IP が入れ替わる


def node_arch(env: Env, node: Node) -> str:
    """自作イメージのアーキテクチャ（LAB_VM_ARCH か、その台の uname -m）。"""
    if env.vm_arch:
        return env.vm_arch
    m = run_on(env, node, "uname -m", timeout=30).strip()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(m, m)


def build_node(env: Env, infra: Infra) -> Node:
    """イメージをビルドする台（SSH の転送先。無ければ 1 番）。"""
    target = forward_target()
    for n in infra.nodes:
        if n.id == target:
            return n
    return sorted(infra.nodes, key=lambda n: n.index)[0]


def _context_tar(context_dir: Path, ca_text: str) -> bytes:
    """ビルドの文脈（__pycache__ と .pyc を除く）に egress-ca.pem（中継の CA。無ければ空）を足した tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(context_dir.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc" or p.name == "egress-ca.pem":
                continue
            tar.add(p, arcname=str(p.relative_to(context_dir)))
        data = ca_text.encode()
        ti = tarfile.TarInfo("egress-ca.pem")
        ti.size = len(data)
        ti.mtime = 0
        tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def build_and_push_on_node(env: Env, infra: Infra, name: str, tag: str, context_dir: Path, build_args: list[str]) -> None:
    """VM 上で docker build し、クラスタの中の置き場（127.0.0.1:<NodePort>。Docker は 127.0.0.0/8 を平文の置き場として扱う）に push する。
    文脈は tar で標準入力から渡す（変更設計書 6 章）。"""
    node = build_node(env, infra)
    arch = node_arch(env, node)
    local = f"{name}:{tag}"
    pushed = f"127.0.0.1:{REGISTRY_NODEPORT}/{name}:{tag}"
    args = " ".join(f"'{a}'" for a in build_args)
    info(f"{node.id}（{arch}）でイメージ {local} をビルドします")
    script = (f"set -e; docker build --platform linux/{arch} {args} -t '{local}' - >/tmp/lab-build-{name}.log 2>&1 "
              f"|| {{ tail -n 60 /tmp/lab-build-{name}.log; exit 1; }}; "
              f"docker tag '{local}' '{pushed}' && docker push -q '{pushed}' && docker rmi '{pushed}' >/dev/null")
    run_on(env, node, script, timeout=1800, input_bytes=_context_tar(context_dir, render.egress_ca_text(env)))
    ok(f"push しました: {REGISTRY_HOST}/{name}:{tag}（{arch}）")


# ---- 中継（出口プロキシ）の VM ----
def init_proxy(env: Env, infra: Infra) -> None:
    """中継の VM を SSH で初期化し（mitmproxy、独自 CA）、CA を .build/egress-ca.pem に取って .env の中継の項目を埋める。"""
    proxy = infra.proxy
    if proxy is None:
        raise LabError("中継の VM がありません（LAB_VM_PROXY_NODE）")
    wait_ssh(env, [proxy])
    script = PROXY_INIT_SCRIPT.read_text().replace("@@PORT@@", str(PROXY_PORT))
    run_on(env, proxy, script, timeout=600)
    ca = run_on(env, proxy, "cat /etc/mitmproxy/mitmproxy-ca-cert.pem", timeout=30)
    if "BEGIN CERTIFICATE" not in ca:
        raise LabError("中継の CA を読めませんでした")
    EGRESS_CA_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(EGRESS_CA_FILE, ca, mode=0o644)
    env.set("LAB_EGRESS_CA_FILE", str(EGRESS_CA_FILE.relative_to(ROOT)))
    env.set("LAB_EGRESS_PROXY", f"http://{proxy.private_ip}:{PROXY_PORT}")
    env.set("LAB_EGRESS_PROXY_MAC", f"http://{proxy.public_ip}:{PROXY_PORT}")
    env.apply_mac_proxy()
    ok(f"中継の VM {proxy.id} の準備ができました（VM と Pod は http://{proxy.private_ip}:{PROXY_PORT}、Mac は http://{proxy.public_ip}:{PROXY_PORT}）")


def proxy_log(env: Env, since: str) -> dict[str, int]:
    """中継の記録（journal）から宛先ホストごとの件数。"""
    infra = load_infra(env)
    if infra.proxy is None:
        raise LabError("中継の VM がありません（LAB_EGRESS_PROXY_VM=true で ./lab up）")
    text = run_on(env, infra.proxy, f"journalctl -u lab-proxy --since '{since}' --no-pager -o cat", timeout=120)
    counts: dict[str, int] = {}
    for line in text.splitlines():
        m = re.search(r"\b(?:GET|POST|PUT|HEAD|DELETE|PATCH|OPTIONS|CONNECT)\s+(https?://[^/\s]+|[^/\s:]+)", line)
        if m:
            host = re.sub(r"^https?://", "", m.group(1)).split(":")[0]
            counts[host] = counts.get(host, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ---- アンインストール（provided の down） ----
def uninstall(env: Env, infra: Infra) -> None:
    """3 台の k3s と keepalived を外し、初期化の印を消す（用意された VM は残す）。"""
    script = ("set +e; /usr/local/bin/k3s-uninstall.sh >/dev/null 2>&1; systemctl disable --now keepalived lab-egress >/dev/null 2>&1; "
              "rm -f /var/lib/lab/ready /var/lib/lab/hooks.sh /etc/keepalived/keepalived.conf; echo uninstalled")
    for node in infra.nodes:
        if node.public_ip and _port_open(node.public_ip, 22):
            run_on(env, node, script, timeout=600)
            info(f"{node.id} の k3s を外しました")
        else:
            warn(f"{node.id} に SSH で届かないので飛ばします")


def empty_unretained(env: Env) -> None:
    """バケットの保持の無いディレクトリ（audit-derived/、pg-backup/、images/、cluster/）を空にする（原本と閲覧記録は残す）。"""
    n = clouds.for_env(env).empty_bucket(env, env.vm_bucket, prefixes=UNRETAINED_PREFIXES)
    info(f"バケット {env.vm_bucket} の {', '.join(UNRETAINED_PREFIXES)} を空にしました（{n} 版）")


# ---- ノードの対応（Kubernetes のノード名 ↔ Node） ----
def node_for_k8s_name(infra: Infra, name: str) -> Node:
    addrs = json.loads(output(kc("get", "node", name, "-o", "jsonpath={.status.addresses}")))
    internal = next(a["address"] for a in addrs if a["type"] == "InternalIP")
    for n in infra.nodes:
        if n.private_ip == internal:
            return n
    raise LabError(f"ノード {name}（{internal}）に当たる VM が見つかりません")


def k8s_name_for_node(node: Node) -> str:
    nodes = json.loads(output(kc("get", "nodes", "-o", "json")))
    for n in nodes["items"]:
        if any(a["type"] == "InternalIP" and a["address"] == node.private_ip for a in n["status"]["addresses"]):
            return n["metadata"]["name"]
    raise LabError(f"VM {node.id}（{node.private_ip}）に当たるノードが見つかりません")


# ---- k3s の版上げ（1 台ずつ。変更設計書 2026-10-26-k3s版上げ 4 章） ----
def node_versions() -> dict[str, str]:
    nodes = json.loads(output(kc("get", "nodes", "-o", "json")))
    return {n["metadata"]["name"]: n["status"]["nodeInfo"]["kubeletVersion"] for n in nodes["items"]}


def etcd_snapshot(env: Env, node: Node, name: str) -> None:
    run_on(env, node, f"/usr/local/bin/k3s etcd-snapshot save --name {name}", timeout=300)
    info(f"etcd のスナップショット {name} を {node.id} に取りました")


def drain_node(node_name: str) -> None:
    run(kc("cordon", node_name), quiet=True)
    run(kc("drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=600s"))


def install_k3s(env: Env, node: Node, version: str) -> None:
    """その台でインストールスクリプトを再実行する（新しい binary を置き、k3s を再起動する）。引数は config.yaml から。
    中継は /etc/environment の値を使う。"""
    run_on(env, node, f"set -a; . /etc/environment; set +a; curl -sfL {K3S_INSTALL_URL} | INSTALL_K3S_VERSION='{version}' sh -s - server",
           timeout=600)


def wait_node_version(node_name: str, version: str) -> None:
    def done() -> bool:
        if node_versions().get(node_name) != version:
            return False
        return succeeds(kc("wait", "--for=condition=Ready", f"node/{node_name}", "--timeout=5s"), timeout=15)
    wait_until(600, f"{node_name} が {version} で Ready になる", done)


def uncordon_node(node_name: str) -> None:
    run(kc("uncordon", node_name), quiet=True)


# ---- A レコードと証明書の退避 ----
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


def zone_candidates(host: str) -> list[str]:
    """ホスト名から、Cloudflare のゾーン名の候補を長い順に（litellm-lab.example.com → [litellm-lab.example.com, example.com]）。"""
    labels = host.strip(".").split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def _cloudflare(env: Env, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{CLOUDFLARE_API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {env.get('LAB_CLOUDFLARE_API_TOKEN')}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        out = json.load(e)
    if not out.get("success"):
        raise LabError(f"Cloudflare の API が失敗しました（{method} {path}）: {out.get('errors')}")
    return out


def ensure_a_record(env: Env, ip: str) -> None:
    """ホスト名 → 浮動 IP の A レコードを Cloudflare に作る（あれば IP を直す。プロキシは使わず DNS のみ、TTL は自動）。
    LAB_CLOUDFLARE_API_TOKEN（Zone / DNS / Edit と Zone / Zone / Read）で足りる（ユーザーの指示 2026-10-30）。"""
    host = env.hostname
    zone = None
    for name in zone_candidates(host):
        found = _cloudflare(env, "GET", f"/zones?name={name}")["result"]
        if found:
            zone = found[0]
            break
    if zone is None:
        raise LabError(f"{host} のゾーンが Cloudflare のトークンで見えません（トークンの Zone Resources にそのゾーンが要ります）")
    records = _cloudflare(env, "GET", f"/zones/{zone['id']}/dns_records?type=A&name={host}")["result"]
    body = {"type": "A", "name": host, "content": ip, "ttl": 1, "proxied": False,
            "comment": "litellm-agent-gateway-lab（./lab が管理）"}
    if not records:
        _cloudflare(env, "POST", f"/zones/{zone['id']}/dns_records", body)
        info(f"Cloudflare に A レコードを作りました: {host} → {ip}（DNS のみ）")
    elif records[0]["content"] != ip or records[0].get("proxied"):
        _cloudflare(env, "PATCH", f"/zones/{zone['id']}/dns_records/{records[0]['id']}", body)
        info(f"Cloudflare の A レコードを直しました: {host} → {ip}（前: {records[0]['content']}）")
    else:
        ok(f"Cloudflare の A レコードは最新です: {host} → {ip}")
    for extra in records[1:]:
        _cloudflare(env, "DELETE", f"/zones/{zone['id']}/dns_records/{extra['id']}")
        info(f"余分な A レコードを消しました: {host} → {extra['content']}")


def wait_for_a_record(env: Env, ip: str) -> None:
    """手持ちの DNS の A レコード（ホスト名 → 浮動 IP）が引けるまで待つ。引けない間は手順を知らせる。"""
    import sys
    host = env.hostname
    start = time.monotonic()
    told = False
    while True:
        try:
            got = {a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET)}
        except socket.gaierror:
            got = set()
        if ip in got:
            ok(f"{host} が {ip} に解決できました")
            return
        if not told:
            info(f"{host} が {ip} に解決できるまで待ちます（Cloudflare に入れた A レコードの反映待ち）")
            told = True
        if time.monotonic() - start > 3600:
            raise LabError(f"{host} が 1 時間たっても {ip} に解決できません（A レコードを確かめてください）")
        time.sleep(10)
        sys.stderr.write(".")
        sys.stderr.flush()


def save_tls_secret() -> None:
    data = output(kn("get", "secret", TLS_SECRET, "-o", "json"), check=False)
    if not data.strip():
        return
    doc = json.loads(data)
    doc["metadata"] = {"name": TLS_SECRET, "namespace": NS, "annotations": {
        k: v for k, v in (doc["metadata"].get("annotations") or {}).items() if k.startswith("cert-manager.io/")}}
    doc.pop("status", None)
    write_if_changed(TLS_BACKUP, json.dumps(doc, indent=2) + "\n", mode=0o600)
    info(f"証明書の Secret {TLS_SECRET} を {TLS_BACKUP.relative_to(ROOT)} に退避しました")


def restore_tls_secret() -> bool:
    if not TLS_BACKUP.exists() or succeeds(kn("get", "secret", TLS_SECRET)):
        return False
    run(kn("apply", "-f", str(TLS_BACKUP)), quiet=True)
    info(f"退避していた証明書の Secret {TLS_SECRET} を戻しました")
    return True
