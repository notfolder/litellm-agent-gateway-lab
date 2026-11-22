"""ECS 構成（LAB_TARGET=aws-ecs）の試験の部品。構築は ecs/ecs-lab.sh が行い、ここは試験に要る読み取りと場面の起こし方だけ
（変更設計書 .history/2026-11-21-ECS構成/ 11 章、13 章）。

- 端末から入口へは、検証の中継（監視の台の mitmproxy）を通る（Env.egress_proxy_mac。core.Env.load が設定する）
- VPC の中にしか無いもの（Valkey の役割）は、監視の台の補助（lab-ecs-api）を SSM のコマンドで呼んで読む
"""

from __future__ import annotations

import json
import time
from functools import lru_cache

from . import aws
from .core import Env, LabError, info


def stack(env: Env) -> str:
    return f"{env.ecs_prefix}-ecs"


@lru_cache(maxsize=4)
def _outputs(profile: str, region: str, name: str) -> dict:
    import boto3
    cf = boto3.Session(profile_name=profile or None, region_name=region).client("cloudformation")
    try:
        st = cf.describe_stacks(StackName=name)["Stacks"][0]
    except Exception as e:  # noqa: BLE001
        raise LabError(f"スタック {name} がありません（ecs/ecs-lab.sh up で作ります）: {e}") from e
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def outputs(env: Env) -> dict:
    return _outputs(env.aws_profile, env.aws_region, stack(env))


def ensure_access(env: Env) -> None:
    """アカウントとバケットの接頭辞を Env に載せる（ecs-lab.sh と同じ <接頭辞>-<アカウント>-）。"""
    if not env.values.get("LAB_ECS_BUCKET_PREFIX"):
        env.values["LAB_ECS_BUCKET_PREFIX"] = f"{env.ecs_prefix}-{aws.identity(env)['Account']}-"


def cluster(env: Env) -> str:
    return outputs(env)["ClusterName"]


def service_tasks(env: Env, service: str, status: str = "RUNNING") -> list[dict]:
    ecs = aws.client(env, "ecs")
    arns = ecs.list_tasks(cluster=cluster(env), serviceName=service, desiredStatus=status).get("taskArns", [])
    return ecs.describe_tasks(cluster=cluster(env), tasks=arns)["tasks"] if arns else []


def stop_one(env: Env, service: str, reason: str) -> str:
    tasks = [t for t in service_tasks(env, service) if t.get("lastStatus") == "RUNNING"]
    if not tasks:
        raise LabError(f"{service} に動いているタスクがありません")
    arn = tasks[0]["taskArn"]
    aws.client(env, "ecs").stop_task(cluster=cluster(env), task=arn, reason=reason)
    return arn.rsplit("/", 1)[-1]


def services_steady(env: Env) -> bool:
    ecs = aws.client(env, "ecs")
    arns = ecs.list_services(cluster=cluster(env))["serviceArns"]
    for i in range(0, len(arns), 10):
        for s in ecs.describe_services(cluster=cluster(env), services=arns[i:i + 10])["services"]:
            if s["runningCount"] != s["desiredCount"] or len(s.get("deployments", [])) != 1:
                return False
    return True


def rds_status(env: Env) -> dict:
    return aws.client(env, "rds").describe_db_instances(DBInstanceIdentifier=outputs(env)["RdsId"])["DBInstances"][0]


def on_monitor(env: Env, command: str, timeout: int = 120) -> str:
    """監視の台でシェルの文を流し、標準出力を返す（SSM の RunShellScript）。"""
    ssm = aws.client(env, "ssm")
    iid = outputs(env).get("MonitorInstanceId")
    if not iid:
        raise LabError("監視の台がありません（provided の台は ecs-lab.sh が台の名前のタグから探します）")
    cid = ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                           Parameters={"commands": [command]})["Command"]["CommandId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            r = ssm.get_command_invocation(CommandId=cid, InstanceId=iid)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if r["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
            if r["Status"] != "Success":
                raise LabError(f"監視の台での実行に失敗しました: {r['StandardErrorContent'][-400:]}")
            return r["StandardOutputContent"]
    raise LabError("監視の台での実行が終わりません")


def valkey_roles(env: Env) -> dict[str, str]:
    """{valkey-N.<ns>: master / slave / down}（監視の台の lab-ecs-api。Zabbix の登録で置かれる）。"""
    return json.loads(on_monitor(env, "sudo -u zabbix /usr/local/bin/lab-ecs-api valkey"))["nodes"]


def valkey_primary(env: Env) -> str:
    roles = valkey_roles(env)
    masters = [h for h, r in roles.items() if r == "master"]
    if len(masters) != 1:
        raise LabError(f"Valkey の primary が 1 台ではありません: {roles}")
    return masters[0]


def healthy(env: Env) -> bool:
    if rds_status(env)["DBInstanceStatus"] != "available" or not services_steady(env):
        return False
    roles = list(valkey_roles(env).values())
    return roles.count("master") == 1 and roles.count("slave") == 2


def crashed_since(env: Env, service: str, t0: float) -> list[dict]:
    """t0 以降に落ちたタスク（コンテナが自分で終わった・起動に失敗した）。自分で止めたもの（UserInitiated）と、
    自動増減や配備で ECS が止めたもの（ServiceSchedulerInitiated）は数えない（2026-11-21: 自動増減の縮小を落ちたと数えていた）。"""
    return [t for t in service_tasks(env, service, "STOPPED")
            if t.get("stoppedAt") and t["stoppedAt"].timestamp() >= t0
            and t.get("stopCode") not in ("UserInitiated", "ServiceSchedulerInitiated")]


def redis_error_times(env: Env, since: float, pattern) -> list[float]:
    """since 以降の LiteLLM のログ（CloudWatch Logs）のうち、Redis のエラーの行の時刻。"""
    logs = aws.client(env, "logs")
    times, token = [], None
    while True:
        kw = {"logGroupName": f"/{env.ecs_prefix}/litellm", "startTime": int(since * 1000), "filterPattern": "?Redis ?redis ?Sentinel"}
        if token:
            kw["nextToken"] = token
        out = logs.filter_log_events(**kw)
        times += [e["timestamp"] / 1000 for e in out.get("events", []) if pattern.search(e["message"])]
        token = out.get("nextToken")
        if not token:
            return times


def trigger(env: Env, scenario: str) -> str:
    if scenario == "litellm-task":
        return f"LiteLLM のタスク {stop_one(env, 'litellm', 'test-failover litellm-task')} を停止"
    if scenario == "pgbouncer-task":
        return f"PgBouncer のタスク {stop_one(env, 'pgbouncer', 'test-failover pgbouncer-task')} を停止"
    if scenario == "rds-failover":
        before = rds_status(env)["AvailabilityZone"]
        aws.client(env, "rds").reboot_db_instance(DBInstanceIdentifier=outputs(env)["RdsId"], ForceFailover=True)
        return f"RDS をフェイルオーバー付きで再起動（primary の AZ: {before}）"
    if scenario == "valkey-primary":
        host = valkey_primary(env)
        service = host.split(".", 1)[0]
        return f"Valkey の primary（{service}）のタスク {stop_one(env, service, 'test-failover valkey-primary')} を停止"
    if scenario == "sentinel-task":
        return f"Sentinel のタスク {stop_one(env, 'sentinel-0', 'test-failover sentinel-task')} を停止"
    raise LabError(f"知らない場面です: {scenario}")


def describe(env: Env) -> str:
    out = outputs(env)
    info(f"ECS: クラスタ {out['ClusterName']}、入口 {env.base_url}（中継 {env.egress_proxy_mac or 'なし'}）")
    return out["ClusterName"]


def run_task(env: Env, family: str, command: list[str], timeout: int = 900) -> str:
    """タスク定義 <接頭辞>-<family> を単発で動かし（コマンドを差し替え）、終わるまで待ってログを返す（Kubernetes の Job の代わり）。"""
    ecs = aws.client(env, "ecs")
    out = outputs(env)
    td = ecs.describe_task_definition(taskDefinition=f"{env.ecs_prefix}-{family}")["taskDefinition"]
    container = td["containerDefinitions"][0]
    arn = ecs.run_task(cluster=cluster(env), taskDefinition=td["taskDefinitionArn"], launchType="FARGATE",
                       networkConfiguration={"awsvpcConfiguration": {
                           "subnets": out["PrivateSubnets"].split(","), "securityGroups": [out["InternalSecurityGroup"]],
                           "assignPublicIp": "DISABLED"}},
                       overrides={"containerOverrides": [{"name": container["name"], "command": command}]})["tasks"][0]["taskArn"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = ecs.describe_tasks(cluster=cluster(env), tasks=[arn])["tasks"][0]
        if t["lastStatus"] == "STOPPED":
            break
        time.sleep(5)
    else:
        raise LabError(f"タスク {family} が {timeout} 秒で終わりません")
    opts = container["logConfiguration"]["options"]
    stream = f"{opts['awslogs-stream-prefix']}/{container['name']}/{arn.rsplit('/', 1)[-1]}"
    logs = aws.client(env, "logs")
    lines: list[str] = []
    for _ in range(10):   # ログは少し遅れて届く
        try:
            ev = logs.get_log_events(logGroupName=opts["awslogs-group"], logStreamName=stream, startFromHead=True)["events"]
            lines = [e["message"] for e in ev]
            if lines:
                break
        except logs.exceptions.ResourceNotFoundException:
            pass
        time.sleep(3)
    code = t["containers"][0].get("exitCode")
    if code != 0:
        raise LabError(f"タスク {family} が失敗しました（終了コード {code}、{t.get('stoppedReason', '')}）:\n" + "\n".join(lines[-40:]))
    return "\n".join(lines)


def restart_service(env: Env, service: str, timeout: int = 900) -> None:
    """サービスのタスクを 1 つずつ入れ替える（kubectl rollout restart の代わり）。"""
    ecs = aws.client(env, "ecs")
    ecs.update_service(cluster=cluster(env), service=service, forceNewDeployment=True)
    ecs.get_waiter("services_stable").wait(cluster=cluster(env), services=[service],
                                           WaiterConfig={"Delay": 10, "MaxAttempts": timeout // 10})


def service_desired(env: Env, service: str) -> int:
    s = aws.client(env, "ecs").describe_services(cluster=cluster(env), services=[service])["services"]
    return s[0]["desiredCount"] if s else 0


PROBE = """
import json, socket, sys, urllib.request
def http(url, proxy=None, timeout=8):
    h = urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {})
    try:
        import ssl; ctx = ssl._create_unverified_context()   # 中継の CA を持たないので、届くかだけを見る
        r = urllib.request.build_opener(h, urllib.request.HTTPSHandler(context=ctx)).open(url, timeout=timeout)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close(); return "open"
    except Exception as e:
        return type(e).__name__
a = json.loads(sys.argv[1])
print("PROBE " + json.dumps({
  "direct": http("https://api.anthropic.com/"),
  "via_proxy": http("https://api.anthropic.com/", a["proxy"]),
  "admin": http(a["base"] + "/ui/"),
  "audit": http(a["base"] + "/lab-audit/"),
  "api": http(a["base"] + "/v1/models"),
  "rds": tcp(a["rds"], 5432),
  "valkey": tcp("valkey-0." + a["ns"], 6379),
  "pgbouncer": tcp(a["pgb"], 5432),
}))
"""


def checks(env: Env) -> None:
    """ECS 構成だけの検証（変更仕様書 7 章の「社内プロキシ」「守り」「更新」）。"""
    from .core import ok, step, warn
    step("test-ecs ECS 構成の守りと更新")
    out = outputs(env)
    pgb = next(d["value"] for t in service_tasks(env, "pgbouncer") for a in t.get("attachments", [])
               for d in a.get("details", []) if d["name"] == "privateIPv4Address")
    args = json.dumps({"proxy": f"http://relay.{out['Namespace']}:3128", "base": f"https://{env.hostname}",
                       "rds": out["RdsEndpoint"], "ns": out["Namespace"], "pgb": pgb})
    # 試験用の偽 LLM のタスク定義（中継の環境変数も DB への許可も無い、ほかのサービスと同じセキュリティグループ）で VPC の中から確かめる
    log = run_task(env, "fake-llm", ["python", "-c", PROBE, args], timeout=300)
    line = next(x for x in log.splitlines() if x.startswith("PROBE "))
    got = json.loads(line[6:])
    info(f"VPC の中からの確かめ: {got}")
    results = []

    def check(desc: str, passed: bool) -> None:
        results.append(passed)
        (ok if passed else warn)(f"{'OK' if passed else 'NG'} {desc}")

    check("タスクから外への直接の HTTPS は届かない", got["direct"] in ("TimeoutError", "URLError", "timeout", "OSError"))
    check("中継を通せば外に届く", isinstance(got["via_proxy"], int))
    check("管理画面（/ui）は踏み台以外からは 403", got["admin"] == 403)
    check("検索アプリ（/lab-audit）は踏み台以外からは 403", got["audit"] == 403)
    check("利用者の API はどこからも届く（キー無しで 401）", got["api"] == 401)
    check("許していないサービスから RDS に届かない", got["rds"] != "open")
    check("許していないサービスから Valkey に届かない", got["valkey"] != "open")
    check("許していないサービスから PgBouncer に届かない", got["pgbouncer"] != "open")
    rollback_check(env, check)
    if not all(results):
        raise LabError(f"ECS 構成の確かめ: {results.count(False)} 件 NG")
    ok(f"ECS 構成の確かめ: {len(results)} 件すべて OK")


def rollback_check(env: Env, check) -> None:
    """起動しない版（設定のキーが無い）を LiteLLM に入れると、配備の遮断器が前の版に戻し、要求は止まらない。"""
    ecs = aws.client(env, "ecs")
    svc = ecs.describe_services(cluster=cluster(env), services=["litellm"])["services"][0]
    good = svc["taskDefinition"]
    td = ecs.describe_task_definition(taskDefinition=good)["taskDefinition"]
    keep = {k: td[k] for k in ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
                               "requiresCompatibilities", "cpu", "memory", "runtimePlatform", "ephemeralStorage") if k in td}
    for e in keep["containerDefinitions"][0]["environment"]:
        if e["name"] == "LITELLM_CONFIG_BUCKET_OBJECT_KEY":
            e["value"] = "litellm/does-not-exist.yaml"
    bad = ecs.register_task_definition(**keep)["taskDefinition"]["taskDefinitionArn"]
    info(f"起動しない版を入れます: {bad.rsplit('/', 1)[-1]}")
    ecs.update_service(cluster=cluster(env), service="litellm", taskDefinition=bad)
    deadline = time.time() + 1500
    state = ""
    while time.time() < deadline:
        time.sleep(20)
        dep = ecs.describe_services(cluster=cluster(env), services=["litellm"])["services"][0]
        primary = next(d for d in dep["deployments"] if d["status"] == "PRIMARY")
        state = primary.get("rolloutState", "")
        if dep["taskDefinition"] == good and state == "COMPLETED":
            break
    ecs.deregister_task_definition(taskDefinition=bad)
    now = ecs.describe_services(cluster=cluster(env), services=["litellm"])["services"][0]["taskDefinition"]
    check(f"起動しない版は配備の遮断器が前の版に戻す（今の版 {now.rsplit('/', 1)[-1]}、{state}）", now == good and state == "COMPLETED")


DELETE_PROBE = """
import json, sys, boto3
from botocore.exceptions import ClientError
a = json.loads(sys.argv[1])
try:
    boto3.client("s3").delete_object(Bucket=a["bucket"], Key=a["key"], VersionId=a["version"])
    print("PROBE deleted")
except ClientError as e:
    print("PROBE " + e.response.get("Error", {}).get("Code", "?"))
"""


def delete_denied(env: Env, role_of_family: str, bucket: str, key: str, version: str) -> bool:
    """タスク定義 <接頭辞>-<role_of_family> のタスクロールで、原本の版を消そうとして拒否されるか（監査のイメージに同じロールを付けた
    一時的なタスク定義で動かす。LiteLLM のイメージは起動の入口を差し替えられないため）。"""
    ecs = aws.client(env, "ecs")
    src = ecs.describe_task_definition(taskDefinition=f"{env.ecs_prefix}-{role_of_family}")["taskDefinition"]
    viewer = ecs.describe_task_definition(taskDefinition=f"{env.ecs_prefix}-audit-viewer")["taskDefinition"]
    container = {k: viewer["containerDefinitions"][0][k] for k in ("image", "logConfiguration")}
    family = f"{env.ecs_prefix}-probe"
    td = ecs.register_task_definition(
        family=family, taskRoleArn=src["taskRoleArn"], executionRoleArn=viewer["executionRoleArn"], networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"], cpu="256", memory="512", runtimePlatform=viewer["runtimePlatform"],
        containerDefinitions=[{"name": "probe", "essential": True, **container,
                               "environment": [{"name": "AWS_DEFAULT_REGION", "value": env.aws_region}]}])["taskDefinition"]
    try:
        log = run_task(env, "probe", ["python", "-c", DELETE_PROBE, json.dumps({"bucket": bucket, "key": key, "version": version})], timeout=300)
    finally:
        ecs.deregister_task_definition(taskDefinition=td["taskDefinitionArn"])
    got = next((x[6:] for x in log.splitlines() if x.startswith("PROBE ")), "")
    info(f"{role_of_family} のロールで原本を消す: {got}")
    return got == "AccessDenied"
