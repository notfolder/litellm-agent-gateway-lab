"""VM 共通層（LAB_TARGET=aws-ec2 / gcp-vm。変更設計書 .history/2026-10-30-VM環境の共通化とGCP追加/ 6 章）。

クラウドを知らない部分: SSH（鍵、22 の許可の更新、コマンドの実行、kubeconfig、6443 のポート転送）、VM の初期化スクリプトの
描画、k3s のトークンと版（バケットの鍵）、静的キーの Secret との同期、k3s の版上げの部品、証明書の退避、A レコードの待ち。
クラウドの API は labtool/clouds/ のモジュール（clouds.for_env(env)）を通してだけ呼ぶ。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import yaml

from . import clouds, render
from .clouds import CLUSTER_BUCKET, PRIORITIES, REGISTRY_HOST, REGISTRY_NODEPORT, TOKEN_KEY, VERSION_KEY, Infra, Node
from .core import (
    BUILD_DIR, K3S_IMAGE, NS, ROOT, VM_KUBE_CONTEXTS, Env, LabError, gen_secret, info, kc, kn, ok, output, run,
    succeeds, wait_until, write_if_changed,
)

SSH_KEY = BUILD_DIR / "vm-ssh-key"
KNOWN_HOSTS = BUILD_DIR / "vm-known-hosts"
FORWARD_PID_FILE = BUILD_DIR / "vm-forward.pid"
FORWARD_TARGET_FILE = BUILD_DIR / "vm-forward.target"   # 転送先の Node.id（版上げでその台を避けるため）
FORWARD_LOG = BUILD_DIR / "vm-forward.log"
INFRA_FILE = BUILD_DIR / "vm-infra.json"                 # 最後に探した基盤（無くても API で探し直せる）
TLS_BACKUP = BUILD_DIR / "vm-tls-secret.json"
TLS_SECRET = "litellm-tls"
NODE_INIT_SCRIPT = ROOT / "vm" / "node-init.sh"
K3S_INSTALL_URL = "https://get.k3s.io"
SSH_USER = "ubuntu"
IP_LOOKUPS = ["https://checkip.amazonaws.com", "https://api.ipify.org"]
# 静的キーを置く Kubernetes の Secret（利用者 → Secret の名前、アクセスキーの項目、シークレットの項目）
KEY_SECRETS = {
    "litellm-writer": ("litellm-secrets", "AUDIT_S3_ACCESS_KEY", "AUDIT_S3_SECRET_KEY"),
    "audit-compactor": ("audit-compactor-s3", "AUDIT_S3_ACCESS_KEY", "AUDIT_S3_SECRET_KEY"),
    "audit-viewer": ("audit-viewer-s3", "AUDIT_S3_ACCESS_KEY", "AUDIT_S3_SECRET_KEY"),
    "pg-backup": ("pg-backup-s3", "ACCESS_KEY_ID", "ACCESS_SECRET_KEY"),
    "registry": (render.VM_REGISTRY_NAME, "REGISTRY_STORAGE_S3_ACCESSKEY", "REGISTRY_STORAGE_S3_SECRETKEY"),
    "operator": ("lab-operator-s3", "ACCESS_KEY", "SECRET_KEY"),
}


def kube_context(env: Env) -> str:
    return VM_KUBE_CONTEXTS[env.target]


# ---- k3s の版（純粋な関数。単体テストの対象。EC2 版の labtool/ec2.py から） ----
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
    """22 を開ける送信元。.env の LAB_SSH_ALLOW_CIDR があればそれ、無ければパブリック IPv4 を外部に問い合わせて /32。"""
    fixed = env.get("LAB_SSH_ALLOW_CIDR", "").strip()
    if fixed:
        return fixed if "/" in fixed else f"{fixed}/32"
    lookup = lookup or _lookup_public_ip
    text = (lookup() or "").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text or ""):
        raise LabError(f"操作者のパブリック IPv4 が分かりません（{text!r}）。.env の LAB_SSH_ALLOW_CIDR に書いてください")
    return f"{text}/32"


def _lookup_public_ip() -> str:
    for url in IP_LOOKUPS:
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                return res.read().decode().strip()
        except Exception:  # noqa: BLE001 次の問い合わせ先へ
            continue
    return ""


def ensure_ssh_key() -> str:
    """SSH の鍵（.build/vm-ssh-key。無ければ作る）。公開鍵の文字列を返す。"""
    if not SSH_KEY.exists():
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "litellm-lab", "-f", str(SSH_KEY)], quiet=True)
        info(f"SSH の鍵を {SSH_KEY.relative_to(ROOT)} に作りました")
    return SSH_KEY.with_suffix(".pub").read_text().strip()


def ssh_base(node: Node, *extra: str) -> list[str]:
    if not node.public_ip:
        raise LabError(f"ノード {node.id} に公開 IP がありません（起動を待つか、外向きの IP を確かめてください）")
    # known_hosts は IP でなく台の識別子で引く（GCP は浮動 IP を取った台の公開 IP が静的 IP に入れ替わり、障害で別の台に移る）
    return ["ssh", "-i", str(SSH_KEY), "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"HostKeyAlias={host_alias(node)}", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
            *extra, f"{SSH_USER}@{node.public_ip}"]


def host_alias(node: Node) -> str:
    """known_hosts の鍵にする台の識別子（GCP の <ゾーン>/<名前> の / は _ に）。"""
    return node.id.replace("/", "_")


def run_on(node: Node, script: str, timeout: int = 120) -> str:
    """VM の中でシェルのスクリプトを root で流し、標準出力を返す（EC2 版の Run Command の置き換え）。"""
    proc = run([*ssh_base(node), "sudo", "bash", "-s"], input=script, capture=True, check=False, timeout=timeout + 30)
    if proc.returncode != 0:
        raise LabError(f"{node.id} でのコマンドが失敗しました（終了コード {proc.returncode}）: {(proc.stderr or proc.stdout or '')[-800:]}")
    return proc.stdout


def forget_host(node: Node) -> None:
    """台を作り直したとき、known_hosts からその台の行を消す。"""
    if KNOWN_HOSTS.exists():
        lines = [line for line in KNOWN_HOSTS.read_text().splitlines() if not line.startswith(host_alias(node) + " ")]
        KNOWN_HOSTS.write_text("\n".join(lines) + ("\n" if lines else ""))


def refresh_public_ips(env: Env, infra: Infra) -> bool:
    """台の公開 IP をクラウドから引き直す（GCP は浮動 IP を取った台の公開 IP が静的 IP に変わる。配備で判明 2026-10-30）。
    変わっていれば .build に保存して True。"""
    found = clouds.for_env(env).discover(env)
    if found is None:
        return False
    by_index = {n.index: n for n in found.nodes}
    changed = False
    for n in infra.nodes:
        fresh = by_index.get(n.index)
        if fresh and fresh.public_ip and fresh.public_ip != n.public_ip:
            info(f"{n.id} の公開 IP が {n.public_ip or '(無し)'} → {fresh.public_ip} に変わりました")
            n.public_ip = fresh.public_ip
            changed = True
    if changed:
        save_infra(infra)
    return changed


def _port_open(ip: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(3)
        return s.connect_ex((ip, port)) == 0


def wait_nodes_ready(env: Env, infra: Infra) -> None:
    """3 台の 22 が開き、初期化（vm/node-init.sh）が終わる（/var/lib/lab/ready）まで待つ。
    届かない台があれば公開 IP を引き直す（主系が浮動 IP を取ると GCP では公開 IP が変わる）。"""
    nodes = infra.nodes

    def reachable() -> bool:
        if all(n.public_ip and _port_open(n.public_ip, 22) for n in nodes):
            return True
        refresh_public_ips(env, infra)
        return False

    wait_until(600, "3 台の SSH（22）", reachable)

    def initialized() -> bool:
        for n in nodes:
            try:
                if run_on(n, "test -f /var/lib/lab/ready && echo ready", timeout=30).strip() != "ready":
                    return False
            except LabError:
                refresh_public_ips(env, infra)
                return False
        return True

    wait_until(900, "3 台の k3s と keepalived の準備（node-init.sh）", initialized)


def fetch_kubeconfig(env: Env, node: Node) -> None:
    """k3s の kubeconfig を 1 台から読み、ポート転送の宛先（127.0.0.1）に向けたコンテキストを ~/.kube/config に書く。"""
    cfg = yaml.safe_load(run_on(node, "cat /etc/rancher/k3s/k3s.yaml", timeout=30))
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
        ssh_base(nodes[0], "-N", "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
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


# ---- 基盤（クラウド固有層の呼び出しのまとめ） ----
def save_infra(infra: Infra) -> None:
    write_if_changed(INFRA_FILE, json.dumps(asdict(infra), indent=2, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)


def load_infra(env: Env) -> Infra:
    """基盤を API で探す（.build の記録は目安にしか使わない）。無ければエラー。"""
    infra = clouds.for_env(env).discover(env)
    if infra is None or not infra.nodes:
        raise LabError(f"{env.target} の基盤がありません。先に ./lab cluster を流してください")
    save_infra(infra)
    return infra


def ensure_access(env: Env) -> None:
    """VM に届く状態にする: 22 の許可を操作者の今の IP に合わせ、ポート転送を確かめる。基盤が無ければ何もしない。"""
    cloud = clouds.for_env(env)
    infra = cloud.discover(env)
    if infra is None or not infra.nodes:
        return
    cloud.allow_ssh_from(env, operator_cidr(env))
    save_infra(infra)
    ensure_forward(env, infra)


def ensure_cluster_objects(env: Env, cloud, infra: Infra) -> str:
    """cluster バケットの k3s/token（無ければ乱数）と k3s/version（無ければコードの版）。配備済みの版とコードの版が
    違えば止める（上げるのは ./lab upgrade-k8s だけ。./lab up では版を変えない）。今の版を返す。"""
    bucket = infra.buckets[CLUSTER_BUCKET]
    if cloud.get_object(env, bucket, TOKEN_KEY) is None:
        cloud.put_object(env, bucket, TOKEN_KEY, gen_secret(32).encode())
        info(f"k3s の参加トークンを {bucket}/{TOKEN_KEY} に置きました")
    version = cloud.get_object(env, bucket, VERSION_KEY)
    if version is None:
        cloud.put_object(env, bucket, VERSION_KEY, k3s_install_version().encode())
        info(f"k3s の版 {k3s_install_version()} を {bucket}/{VERSION_KEY} に置きました")
        return k3s_install_version()
    deployed = version.decode().strip()
    if deployed != k3s_install_version():
        raise LabError(f"配備済みの k3s の版（{deployed}）とコードの版（{k3s_install_version()}）が違います。"
                       f"上げるなら ./lab upgrade-k8s {k3s_install_version()}、戻すなら core.py の K3S_IMAGE を "
                       f"{deployed} に合わせてください（./lab up では版を変えません）")
    return deployed


def set_version(env: Env, cloud, infra: Infra, version: str) -> None:
    cloud.put_object(env, infra.buckets[CLUSTER_BUCKET], VERSION_KEY, version.encode())


def render_node_init(node: Node, all_nodes: list[Node], hooks: str, pubkey: str, script: str | None = None) -> str:
    """vm/node-init.sh の @@名前@@ をこの台の値で置き換え、クラウド固有の hooks を差し込む。"""
    text = script if script is not None else NODE_INIT_SCRIPT.read_text()
    ordered = sorted(all_nodes, key=lambda n: n.index)
    peers = [n.private_ip for n in ordered if n.index != node.index]
    values = {
        "NODE_INDEX": str(node.index), "SELF_IP": node.private_ip, "FIRST_IP": ordered[0].private_ip,
        "PEER_IPS": " ".join(peers), "PRIORITY": str(PRIORITIES[node.index - 1]), "REGISTRY_HOST": REGISTRY_HOST,
        "REGISTRY_NODEPORT": str(REGISTRY_NODEPORT), "SSH_PUBKEY": pubkey, "HOOKS": hooks,
    }
    for k, v in values.items():
        text = text.replace(f"@@{k}@@", v)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise ValueError(f"node-init.sh に置き換えていない名前が残っています: {left}")
    return text


def user_data_renderer(env: Env, cloud, infra: Infra, pubkey: str):
    """clouds.ensure_nodes に渡す描画関数。"""
    def render_for(node: Node, all_nodes: list[Node]) -> str:
        return render_node_init(node, all_nodes, cloud.node_init_hooks(env, infra, node), pubkey)
    return render_for


# ---- 静的キー（Kubernetes の Secret にだけ置く） ----
def _read_secret(name: str) -> dict[str, str]:
    text = output(kn("get", "secret", name, "--ignore-not-found", "-o", "json"), check=False)
    if not text.strip():
        return {}
    data = json.loads(text).get("data", {}) or {}
    return {k: base64.b64decode(v).decode() for k, v in data.items()}


def drop_env_static_keys(env: Env) -> list[str]:
    """VM の環境では .env の静的キー（S3_<利用者>_KEY / _SECRET、RUSTFS_*）を読まない。Mac の RustFS 用の値が同じ名前で
    .env に残っていて、クラウドの Secret に写してしまうため（配備で判明 2026-10-30）。消した名前を返す。"""
    dropped = []
    names = [n for user in KEY_SECRETS for n in render.s3_env_names(user)] + ["RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY"]
    for name in names:
        if env.values.pop(name, None):
            dropped.append(name)
    return dropped


def load_static_keys(env: Env) -> set[str]:
    """Kubernetes の Secret にある静的キーを Env に載せる（S3_<利用者>_KEY / _SECRET。.env には書かない）。見つかった利用者を返す。
    同じ実行で一度載せた値は使い回す。"""
    found = set()
    for user, (secret, key_item, secret_item) in KEY_SECRETS.items():
        key_name, secret_name = render.s3_env_names(user)
        if user in _loaded_users and env.get(key_name) and env.get(secret_name):
            found.add(user)
            continue
        data = _read_secret(secret)
        if data.get(key_item) and data.get(secret_item):
            env.values[key_name], env.values[secret_name] = data[key_item], data[secret_item]
            found.add(user)
            _loaded_users.add(user)
    return found


_loaded_users: set[str] = set()


def ensure_static_keys(env: Env, cloud, infra: Infra) -> None:
    """静的キーをそろえる: Secret にある利用者はそのまま、無い利用者だけクラウドで作り直して Env に載せる。
    操作者の HMAC キー（GCP だけ）は Secret lab-operator-s3 に書く（他の Secret は stages.secrets_ が書く）。"""
    from .core import kapply, secret_manifest
    have = load_static_keys(env)
    new = cloud.ensure_static_keys(env, infra.bucket_prefix, have)
    for user, (key, secret) in new.items():
        key_name, secret_name = render.s3_env_names(user)
        env.values[key_name], env.values[secret_name] = key, secret
        _loaded_users.add(user)
    op_key, op_secret = render.s3_env_names("operator")
    if env.get(op_key) and not _read_secret(KEY_SECRETS["operator"][0]):
        kapply(manifest=secret_manifest(KEY_SECRETS["operator"][0],
                                        {"ACCESS_KEY": env.get(op_key), "SECRET_KEY": env.get(op_secret)}))


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


def etcd_snapshot(node: Node, name: str) -> None:
    run_on(node, f"/usr/local/bin/k3s etcd-snapshot save --name {name}", timeout=300)
    info(f"etcd のスナップショット {name} を {node.id} に取りました")


def drain_node(node_name: str) -> None:
    run(kc("cordon", node_name), quiet=True)
    run(kc("drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=600s"))


def install_k3s(node: Node, version: str) -> None:
    """その台でインストールスクリプトを再実行する（新しい binary を置き、k3s を再起動する）。引数は config.yaml から。"""
    run_on(node, f"curl -sfL {K3S_INSTALL_URL} | INSTALL_K3S_VERSION='{version}' sh -s - server", timeout=600)


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
