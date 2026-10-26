"""AWS の EC2 3 台と S3 だけの環境（LAB_TARGET=aws-ec2）だけで使う部品: CloudFormation の配備と出力、
Session Manager 経由の接続（コマンドの実行、k3s の API へのポート転送）、kubeconfig、静的キーの受け渡し、
障害試験でのノードの停止、削除（変更設計書 .history/2026-10-25-EC2三台構成/ 4.2 節）。

- AWS の基盤は labtool/cfn.py が組むテンプレート（JSON）を aws cloudformation deploy で配備する
- AWS の API は boto3。AWS CLI は cloudformation deploy と ssm start-session（ポート転送。Session Manager plugin が要る）に使う
- 資格情報は EKS 版と同じ .env の LAB_AWS_PROFILE（labtool/aws.py の session を借りる）
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import re

import yaml

from . import aws, cfn, render
from .core import (
    BUILD_DIR, EC2_KUBE_CONTEXT, NS, Env, LabError, gen_secret, info, kc, kn, ok, output, run, succeeds,
    wait_until, warn, write_if_changed,
)

OUTPUTS_FILE = BUILD_DIR / "ec2-outputs.json"
CFN_DIR = BUILD_DIR / "cfn"
FORWARD_PID_FILE = BUILD_DIR / "ec2-forward.pid"
FORWARD_TARGET_FILE = BUILD_DIR / "ec2-forward.target"   # 転送先のインスタンス ID（版上げでその台を避けるため）
FORWARD_LOG = BUILD_DIR / "ec2-forward.log"
K3S_INSTALL_URL = "https://get.k3s.io"
TLS_BACKUP = BUILD_DIR / "ec2-tls-secret.json"
TLS_SECRET = "litellm-tls"


def stack_name(env: Env, stack: str) -> str:
    return f"{env.ec2_prefix}-{stack}"


def token_parameter(env: Env) -> str:
    """k3s の参加トークンを置く Systems Manager のパラメータ（SecureString。ノードがインスタンスロールで読む）。"""
    return f"/{env.ec2_prefix}/k3s-token"


def version_parameter(env: Env) -> str:
    """k3s の版を置く Systems Manager のパラメータ（String）。node-init.sh が読み、./lab upgrade-k8s が書き換える。
    user data に版を書かないので、版を上げても nodes スタックに差分が出ない（変更設計書 2026-10-26-k3s版上げ 3 章）。"""
    return f"/{env.ec2_prefix}/k3s-version"


def s3_endpoint(env: Env) -> str:
    return f"https://s3.{env.aws_region}.amazonaws.com"


# ---- k3s の版（純粋な関数。単体テストの対象） ----
def parse_k3s_version(text: str) -> tuple[int, int, int]:
    """v1.35.5+k3s1（または v1.35.5-k3s1、kubelet が報告する v1.35.5+k3s1）→ (1, 35, 5)。"""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)[+-]k3s\d+", text.strip())
    if not m:
        raise LabError(f"k3s の版の形が違います: {text!r}（例 v1.36.1+k3s1）")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def normalize_k3s_version(text: str) -> str:
    """インストールスクリプトに渡す形（v1.36.1+k3s1）にそろえる。"""
    parse_k3s_version(text)
    return text.strip().replace("-k3s", "+k3s")


def check_upgrade_step(current: str, target: str) -> None:
    """Kubernetes の版の差の規則: マイナー版は 1 つずつ、下げない（同じマイナー版の中のパッチの更新は可）。"""
    cur, tgt = parse_k3s_version(current), parse_k3s_version(target)
    if tgt[:2] < cur[:2] or (tgt[:2] == cur[:2] and tgt[2] < cur[2]):
        raise LabError(f"{target} は今の {current} より古い版です（下げる操作は行いません）")
    if tgt[0] != cur[0] or tgt[1] > cur[1] + 1:
        raise LabError(f"{current} → {target} はマイナー版を飛ばしています。1 つずつ上げてください（例 v{cur[0]}.{cur[1] + 1}.x+k3s1）")


def upgrade_order(instance_ids: list[str], eip_holder: str | None, forward_target: str | None = None) -> list[str]:
    """上げる順。Elastic IP を持つ台は最後（入口の移動を 1 回にする）。転送先の台はその前（転送の張り直しを 1 回にする）。"""
    rest = [i for i in instance_ids if i not in (eip_holder, forward_target)]
    tail = [i for i in (forward_target, eip_holder) if i and i in instance_ids]
    return rest + list(dict.fromkeys(tail))


# ---- CloudFormation ----
def ensure_token_parameter(env: Env) -> None:
    ssm = aws.client(env, "ssm")
    name = token_parameter(env)
    try:
        ssm.get_parameter(Name=name)
        return
    except ssm.exceptions.ParameterNotFound:
        pass
    ssm.put_parameter(Name=name, Type="SecureString", Value=gen_secret(32),
                      Description="litellm-lab aws-ec2: k3s join token (./lab cluster が管理)")
    info(f"k3s の参加トークンを SSM のパラメータ {name} に置きました")


def ensure_version_parameter(env: Env) -> str:
    """k3s の版のパラメータ。無ければコードの版（core.K3S_IMAGE）で作る。あれば今の値を返す（上書きしない。
    上げるのは ./lab upgrade-k8s だけ）。"""
    ssm = aws.client(env, "ssm")
    name = version_parameter(env)
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        pass
    version = cfn.k3s_install_version()
    ssm.put_parameter(Name=name, Type="String", Value=version,
                      Description="litellm-lab aws-ec2: k3s version (./lab upgrade-k8s が更新)")
    info(f"k3s の版 {version} を SSM のパラメータ {name} に置きました")
    return version


def set_version_parameter(env: Env, version: str) -> None:
    aws.client(env, "ssm").put_parameter(Name=version_parameter(env), Type="String", Value=version, Overwrite=True)


def resolve_ami(env: Env) -> str:
    """nodes スタックに渡す AMI ID。配備済みならその台と同じ値（置き換えを起こさない）、初回は SSM の公開パラメータ
    （AL2023 arm64 の最新）を解く。AMI を新しくするのは、台を 1 台ずつ作り直す別の作業にする。"""
    cf = aws.client(env, "cloudformation")
    try:
        stack = cf.describe_stacks(StackName=stack_name(env, "nodes"))["Stacks"][0]
        current = {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters", [])}
        if current.get("AmiId"):
            return current["AmiId"]
    except Exception:  # noqa: BLE001 無いスタック
        pass
    ami = aws.client(env, "ssm").get_parameter(Name=cfn.AMI_PARAMETER)["Parameter"]["Value"]
    info(f"AMI を {cfn.AMI_PARAMETER} から解きました: {ami}（以後の配備もこの値を使う）")
    return ami


def _deploy_stack(env: Env, stack: str, template: Path, params: dict[str, str] | None = None) -> None:
    name = stack_name(env, stack)
    info(f"CloudFormation のスタック {name} を配備します")
    cmd = ["aws", "cloudformation", "deploy", "--stack-name", name, "--template-file", str(template),
           "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset"]
    if params:
        cmd += ["--parameter-overrides", *[f"{k}={v}" for k, v in params.items()]]
    run(cmd, env_vars=aws.shell_env(env))


def stack_outputs(env: Env, stack: str) -> dict[str, str] | None:
    cf = aws.client(env, "cloudformation")
    try:
        res = cf.describe_stacks(StackName=stack_name(env, stack))
    except Exception:  # noqa: BLE001 無いスタック
        return None
    return {o["OutputKey"]: o["OutputValue"] for o in res["Stacks"][0].get("Outputs", []) or []}


def deploy(env: Env, ident: dict) -> dict:
    """4 スタックを eip → storage → network → nodes の順に配備し、出力をまとめて返す。差分が無ければ何もしない。"""
    ensure_token_parameter(env)
    deployed_version = ensure_version_parameter(env)
    if deployed_version != cfn.k3s_install_version():
        raise LabError(f"配備済みの k3s の版（{deployed_version}）とコードの版（{cfn.k3s_install_version()}）が違います。"
                       f"上げるなら ./lab upgrade-k8s {cfn.k3s_install_version()}、戻すなら core.py の K3S_IMAGE を "
                       f"{deployed_version} に合わせてください（./lab up では版を変えません）")
    CFN_DIR.mkdir(parents=True, exist_ok=True)
    _deploy_stack(env, "eip", cfn.write_template(cfn.eip_template(env), CFN_DIR / "eip.json"))
    eip = stack_outputs(env, "eip") or {}
    docs = cfn.templates(env, ident["Account"], token_parameter(env), eip["EipAllocationId"], version_parameter(env))
    _deploy_stack(env, "storage", cfn.write_template(docs["storage"], CFN_DIR / "storage.json"))
    _deploy_stack(env, "network", cfn.write_template(docs["network"], CFN_DIR / "network.json"))
    net = stack_outputs(env, "network") or {}
    params = {"SecurityGroupId": net["SecurityGroupId"], "InstanceType": env.ec2_instance_type,
              "AmiId": resolve_ami(env)}
    for i in range(1, cfn.NODE_COUNT + 1):
        params[f"Subnet{i}Id"] = net[f"Subnet{i}Id"]
    _deploy_stack(env, "nodes", cfn.write_template(docs["nodes"], CFN_DIR / "nodes.json"), params)
    out = outputs(env)
    ok(f"CloudFormation の配備が終わりました（入口の IP {out['EipPublicIp']}）")
    return out


def outputs(env: Env) -> dict:
    merged: dict[str, str] = {}
    for s in cfn.STACKS:
        merged.update(stack_outputs(env, s) or {})
    if not merged:
        raise LabError(f"CloudFormation の出力がありません（スタック {env.ec2_prefix}-*）。先に ./lab cluster を流してください")
    write_if_changed(OUTPUTS_FILE, json.dumps(merged, indent=2, sort_keys=True) + "\n", mode=0o600)
    return merged


def load_outputs(env: Env) -> dict:
    if OUTPUTS_FILE.exists():
        return json.loads(OUTPUTS_FILE.read_text())
    return outputs(env)


def instance_ids(out: dict) -> list[str]:
    return [x for x in out.get("InstanceIds", "").split(",") if x]


def load_credentials(env: Env, out: dict) -> None:
    """IAM ユーザーの静的キー（CloudFormation の出力）を Env に載せる。.env には書かない。
    S3 の利用者は Mac の RustFS と同じ名前（S3_<利用者>_KEY / _SECRET）、Bedrock は BEDROCK_ACCESS_KEY_ID / _SECRET_ACCESS_KEY。"""
    for user in (*render.S3_USERS, cfn.REGISTRY_USER):
        key_name, secret_name = render.s3_env_names(user)
        env.values[key_name] = out[cfn.output_key(user, "AccessKeyId")]
        env.values[secret_name] = out[cfn.output_key(user, "SecretAccessKey")]
    env.values["BEDROCK_ACCESS_KEY_ID"] = out[cfn.output_key(cfn.BEDROCK_USER, "AccessKeyId")]
    env.values["BEDROCK_SECRET_ACCESS_KEY"] = out[cfn.output_key(cfn.BEDROCK_USER, "SecretAccessKey")]


# ---- Session Manager（Run Command とポート転送） ----
def ssm_online(env: Env, ids: list[str]) -> list[str]:
    """SSM に Online で登録されているインスタンス（渡した順）。"""
    if not ids:
        return []
    res = aws.client(env, "ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": ids}])
    online = {i["InstanceId"] for i in res.get("InstanceInformationList", []) if i.get("PingStatus") == "Online"}
    return [i for i in ids if i in online]


def run_on(env: Env, instance_id: str, commands: list[str], timeout: int = 120) -> str:
    """インスタンスの中でシェルのコマンドを流し、標準出力を返す（Run Command。SSH を開けない代わり）。"""
    ssm = aws.client(env, "ssm")
    cmd_id = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                              Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
                              TimeoutSeconds=max(30, min(timeout, 600)))["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 60
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        if inv["Status"] != "Success":
            raise LabError(f"{instance_id} でのコマンドが失敗しました（{inv['Status']}）: "
                           f"{(inv.get('StandardErrorContent') or inv.get('StandardOutputContent') or '')[-800:]}")
        return inv.get("StandardOutputContent", "")
    raise LabError(f"{instance_id} でのコマンドが {timeout} 秒で終わりませんでした")


def wait_nodes_ready(env: Env, out: dict) -> None:
    """3 台が SSM で Online になり、cloud-init（node-init.sh）が終わる（/var/lib/lab/ready）まで待つ。"""
    ids = instance_ids(out)
    wait_until(600, "3 台が Session Manager に登録される", lambda: len(ssm_online(env, ids)) == len(ids))

    def initialized() -> bool:
        for i in ids:
            try:
                if run_on(env, i, ["test -f /var/lib/lab/ready && echo ready"], timeout=30).strip() != "ready":
                    return False
            except LabError:
                return False
        return True

    wait_until(900, "3 台の k3s と keepalived の準備（node-init.sh）", initialized)


def fetch_kubeconfig(env: Env, instance_id: str) -> None:
    """k3s の kubeconfig を 1 台から読み、ポート転送の宛先（127.0.0.1）に向けたコンテキスト litellm-lab-ec2 を
    ~/.kube/config に書く（kubectl config set-* は何度流しても同じ）。"""
    text = run_on(env, instance_id, ["cat /etc/rancher/k3s/k3s.yaml"], timeout=30)
    cfg = yaml.safe_load(text)
    cluster = cfg["clusters"][0]["cluster"]
    user = cfg["users"][0]["user"]
    import base64
    with tempfile.TemporaryDirectory() as d:
        files = {}
        for name, value in (("ca", cluster["certificate-authority-data"]), ("cert", user["client-certificate-data"]),
                            ("key", user["client-key-data"])):
            p = Path(d) / name
            p.write_bytes(base64.b64decode(value))
            files[name] = str(p)
        run(["kubectl", "config", "set-cluster", EC2_KUBE_CONTEXT, f"--server=https://127.0.0.1:{env.ec2_local_port}",
             f"--certificate-authority={files['ca']}", "--embed-certs=true"], quiet=True)
        run(["kubectl", "config", "set-credentials", EC2_KUBE_CONTEXT, f"--client-certificate={files['cert']}",
             f"--client-key={files['key']}", "--embed-certs=true"], quiet=True)
        run(["kubectl", "config", "set-context", EC2_KUBE_CONTEXT, f"--cluster={EC2_KUBE_CONTEXT}",
             f"--user={EC2_KUBE_CONTEXT}"], quiet=True)
    ok(f"kubectl の context {EC2_KUBE_CONTEXT} を更新しました（{instance_id} から）")


def forward_alive(env: Env) -> bool:
    return succeeds(kc("get", "--raw", "/readyz", "--request-timeout=5s"), timeout=15)


def _port_listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def forward_target() -> str | None:
    """今の転送先のインスタンス ID（分からなければ None）。"""
    return FORWARD_TARGET_FILE.read_text().strip() or None if FORWARD_TARGET_FILE.exists() else None


def ensure_forward(env: Env, avoid: str | None = None) -> None:
    """k3s の API への Session Manager のポート転送を確かめ、無ければ張る。転送は背景に残し（./lab を何度も流すため）、
    ./lab down で止める。Session Manager の既定では 20 分使わないと切れるので、切れていれば張り直す。
    avoid の台（版上げで k3s を再起動する台）に張ってあれば、別の台へ張り直す。"""
    if forward_alive(env) and (avoid is None or forward_target() != avoid):
        return
    out = load_outputs(env)
    ids = [i for i in ssm_online(env, instance_ids(out)) if i != avoid]
    if not ids:
        raise LabError("Session Manager に Online のノードがありません（./lab cluster で作るか、起動を待ってください）")
    stop_forward(env)
    fetch_kubeconfig(env, ids[0])
    port = env.ec2_local_port
    if _port_listening(port):
        raise LabError(f"ローカルのポート {port} が別のプロセスに使われています（.env の LAB_EC2_LOCAL_PORT を変えてください）")
    FORWARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(FORWARD_LOG, "ab")
    proc = subprocess.Popen(
        ["aws", "ssm", "start-session", "--target", ids[0], "--document-name", "AWS-StartPortForwardingSession",
         "--parameters", json.dumps({"portNumber": ["6443"], "localPortNumber": [str(port)]})],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=aws.shell_env(env))
    FORWARD_PID_FILE.write_text(str(proc.pid))
    FORWARD_TARGET_FILE.write_text(ids[0])
    info(f"Session Manager のポート転送を張ります（{ids[0]} の 6443 → 127.0.0.1:{port}）")
    wait_until(90, "k3s の API への接続", lambda: proc.poll() is None and forward_alive(env))


def stop_forward(env: Env) -> None:
    FORWARD_TARGET_FILE.unlink(missing_ok=True)
    if not FORWARD_PID_FILE.exists():
        return
    try:
        pid = int(FORWARD_PID_FILE.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    FORWARD_PID_FILE.unlink(missing_ok=True)


@contextlib.contextmanager
def registry_forward(env: Env):
    """クラスタの中のイメージ置き場（registry）に Mac からつなぐ。with の中では http://127.0.0.1:<ポート>。"""
    from .core import port_forward
    with port_forward(f"svc/{render.EC2_REGISTRY_NAME}", 5000) as endpoint:
        yield endpoint


# ---- ノードの停止（障害の試験）と Elastic IP ----
def instance_id_for_node(env: Env, out: dict, node: str) -> str:
    """Kubernetes のノード名（ip-10-60-1-10.…）→ インスタンス ID（プライベート IP で突き合わせる）。"""
    addrs = json.loads(output(kc("get", "node", node, "-o", "jsonpath={.status.addresses}")))
    internal = next(a["address"] for a in addrs if a["type"] == "InternalIP")
    res = aws.client(env, "ec2").describe_instances(InstanceIds=instance_ids(out))
    for r in res["Reservations"]:
        for inst in r["Instances"]:
            if inst.get("PrivateIpAddress") == internal:
                return inst["InstanceId"]
    raise LabError(f"ノード {node}（{internal}）に当たるインスタンスが見つかりません")


def node_for_instance(env: Env, instance_id: str) -> str:
    inst = aws.client(env, "ec2").describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    ip = inst["PrivateIpAddress"]
    nodes = json.loads(output(kc("get", "nodes", "-o", "json")))
    for n in nodes["items"]:
        if any(a["type"] == "InternalIP" and a["address"] == ip for a in n["status"]["addresses"]):
            return n["metadata"]["name"]
    raise LabError(f"インスタンス {instance_id}（{ip}）に当たるノードが見つかりません")


def stop_node(env: Env, instance_id: str) -> None:
    ec2 = aws.client(env, "ec2")
    ec2.stop_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def start_node(env: Env, instance_id: str) -> None:
    ec2 = aws.client(env, "ec2")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def eip_holder(env: Env, out: dict) -> str | None:
    """Elastic IP が付いているインスタンス ID（付いていなければ None）。"""
    res = aws.client(env, "ec2").describe_addresses(AllocationIds=[out["EipAllocationId"]])
    return (res["Addresses"][0].get("InstanceId") or None) if res["Addresses"] else None


# ---- k3s の版上げ（1 台ずつ。変更設計書 2026-10-26-k3s版上げ 4 章） ----
def node_versions() -> dict[str, str]:
    """ノード名 → kubelet の版（例 v1.35.5+k3s1）。"""
    nodes = json.loads(output(kc("get", "nodes", "-o", "json")))
    return {n["metadata"]["name"]: n["status"]["nodeInfo"]["kubeletVersion"] for n in nodes["items"]}


def etcd_snapshot(env: Env, instance_id: str, name: str) -> None:
    """上げる前の控え（k3s の etcd のスナップショット。その台の /var/lib/rancher/k3s/server/db/snapshots/）。"""
    run_on(env, instance_id, [f"/usr/local/bin/k3s etcd-snapshot save --name {name}"], timeout=300)
    info(f"etcd のスナップショット {name} を {instance_id} に取りました")


def drain_node(node: str) -> None:
    """Pod を他の台へ追い出す。PDB（PostgreSQL、Valkey、LiteLLM）を守るので 1 つずつ動き、CloudNativePG は primary なら
    先に切り替える。local-path の Pod は台が戻るまで Pending で待つ（追い出しの対象にはなる）。"""
    run(kc("cordon", node), quiet=True)
    run(kc("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=600s"))


def install_k3s(env: Env, instance_id: str, version: str) -> None:
    """その台でインストールスクリプトを再実行する（新しい binary を置き、k3s を再起動する。Pod のコンテナは動き続ける）。
    引数は /etc/rancher/k3s/config.yaml にあるので、server だけを渡す（node-init.sh と同じ形）。"""
    run_on(env, instance_id, [f"curl -sfL {K3S_INSTALL_URL} | INSTALL_K3S_VERSION='{version}' sh -s - server"],
           timeout=600)


def wait_node_version(node: str, version: str) -> None:
    def done() -> bool:
        if node_versions().get(node) != version:
            return False
        return succeeds(kc("wait", "--for=condition=Ready", f"node/{node}", "--timeout=5s"), timeout=15)
    wait_until(600, f"{node} が {version} で Ready になる", done)


def uncordon_node(node: str) -> None:
    run(kc("uncordon", node), quiet=True)


def wait_for_a_record(env: Env, ip: str) -> None:
    """手持ちの DNS の A レコード（ホスト名 → Elastic IP）が引けるまで待つ。引けない間は手順を知らせる。"""
    host = env.aws_hostname
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
            warn(f"手持ちの DNS（Cloudflare）に A レコードを足してください: {host}  →  {ip}（プロキシは使わず DNS のみ）\n"
                 "  （反映されるまで待ちます。Ctrl-C で中断しても、もう一度 ./lab up を流せば続きから進みます）")
            told = True
        if time.monotonic() - start > 3600:
            raise LabError(f"{host} が 1 時間たっても {ip} に解決できません（A レコードを確かめてください）")
        time.sleep(10)
        sys.stderr.write(".")
        sys.stderr.flush()


# ---- 証明書の退避（Let's Encrypt の発行数の上限を避ける） ----
def save_tls_secret(env: Env) -> None:
    data = output(kn("get", "secret", TLS_SECRET, "-o", "json"), check=False)
    if not data.strip():
        return
    doc = json.loads(data)
    doc["metadata"] = {"name": TLS_SECRET, "namespace": NS, "annotations": {
        k: v for k, v in (doc["metadata"].get("annotations") or {}).items() if k.startswith("cert-manager.io/")}}
    doc.pop("status", None)
    write_if_changed(TLS_BACKUP, json.dumps(doc, indent=2) + "\n", mode=0o600)
    info(f"証明書の Secret {TLS_SECRET} を {TLS_BACKUP.relative_to(BUILD_DIR.parent)} に退避しました")


def restore_tls_secret(env: Env) -> bool:
    """退避した証明書があり、クラスタに無ければ戻す（cert-manager は有効な証明書があれば発行し直さない）。"""
    if not TLS_BACKUP.exists() or succeeds(kn("get", "secret", TLS_SECRET)):
        return False
    run(kn("apply", "-f", str(TLS_BACKUP)), quiet=True)
    info(f"退避していた証明書の Secret {TLS_SECRET} を戻しました")
    return True


# ---- 削除 ----
def empty_buckets(env: Env, out: dict) -> None:
    """S3 のバケットを空にする（CloudFormation は空でないバケットを消せない）。Object Lock は保持を越えて消す。
    COMPLIANCE のバケットは誰にも消せないので残して知らせる。"""
    s3 = aws.client(env, "s3")
    lock_mode = env.get("LAB_AUDIT_LOCK_MODE", "GOVERNANCE")
    for key, bucket in sorted(out.items()):
        if not key.endswith("BucketName"):
            continue
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            continue
        if lock_mode == "COMPLIANCE" and bucket.endswith((render.AUDIT_RAW, render.AUDIT_ACCESS)):
            warn(f"バケット {bucket} は Object Lock（COMPLIANCE）のため消せません。保持期間が切れるまで残ります"
                 "（storage スタックの削除もそこで止まります）")
            continue
        n = aws.empty_bucket(s3, bucket)
        info(f"バケット {bucket} を空にしました（{n} 版）")


def destroy(env: Env, keep_eip: bool) -> None:
    """nodes → network → storage →（eip）の順にスタックを消す。先にポート転送を止め、S3 を空にする。"""
    stop_forward(env)
    out = {}
    for s in cfn.STACKS:
        out.update(stack_outputs(env, s) or {})
    if out:
        empty_buckets(env, out)
    cf = aws.client(env, "cloudformation")
    order = ["nodes", "network", "storage"] + ([] if keep_eip else ["eip"])
    for stack in order:
        name = stack_name(env, stack)
        if stack_outputs(env, stack) is None:
            continue
        info(f"スタック {name} を消します")
        cf.delete_stack(StackName=name)
        cf.get_waiter("stack_delete_complete").wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
        ok(f"スタック {name} を消しました")
    ssm = aws.client(env, "ssm")
    for name in (token_parameter(env), version_parameter(env)):
        with contextlib.suppress(ssm.exceptions.ParameterNotFound):
            ssm.delete_parameter(Name=name)
            info(f"SSM のパラメータ {name} を消しました")
    OUTPUTS_FILE.unlink(missing_ok=True)
    if keep_eip:
        eip = stack_outputs(env, "eip") or {}
        info(f"Elastic IP {eip.get('EipPublicIp', '?')} は残しました（LAB_EC2_KEEP_EIP=true。付いていない間も料金が出ます）")
