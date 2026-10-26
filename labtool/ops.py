"""構築の段階以外の操作: 削除、状態の表示、更新。"""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import stages
from .core import (
    BUILD_DIR, CLUSTER, EC2_KUBE_CONTEXT, NS, ROOT, Env, LabError, cluster_exists, helm, http_ok,
    info, kn, ok, output, run, step, succeeds, warn,
)


def down() -> None:
    """Mac: クラスタを削除する。DB（クラスタ内）は消える。
    残すもの: .env、ChatGPT のトークン（LAB_DATA_DIR）、イメージの置き場（自作イメージと Docker Hub のキャッシュ）。
    AWS: 有料のものをすべて消す（compute → data → network のスタックと、残る S3・秘密情報・スナップショット）。
    残すのは無料のもの: ACM の証明書（cert スタック。消すと再検証が要る）、Identity Center、DNS の CNAME、
    CDK の bootstrap（変更仕様書 2026-10-24-AWS構成 6 章）。"""
    step("down クラスタの削除")
    env = Env.load()
    if env.is_aws:
        from . import aws
        # Ingress を先に消して ALB を片付けさせる（残っていると compute のスタックの削除が止まる）
        if succeeds(kn("get", "ingress", "litellm"), timeout=60):
            info("ALB の入口（Ingress）を消します")
            run(kn("delete", "ingress", "litellm", "--wait=true", "--timeout=300s"), check=False)
        aws.destroy(env, ["compute", "data", "network"])
        aws.purge_retained(env)
        ok(f"AWS の有料リソースを削除しました（残るのは無料の {env.aws_prefix}-cert の証明書と CDK の bootstrap）")
        return
    if env.is_aws_ec2:
        from . import ec2
        # 証明書を退避してから（Let's Encrypt の発行数の上限を避ける）、nodes → network → storage →（eip）を消す。
        # IAM ユーザーとキーは storage と一緒に消える。残るのは DNS の A レコードと .build/ の退避だけ
        if ec2.OUTPUTS_FILE.exists():
            try:
                ec2.ensure_forward(env)
                ec2.save_tls_secret(env)
            except LabError as e:
                warn(f"証明書の退避を飛ばします（{e}）")
        ec2.destroy(env, keep_eip=env.ec2_keep_eip)
        ok("AWS の有料リソースを削除しました（EC2、EBS、S3、IAM ユーザー"
           + ("。Elastic IP は残しました）" if env.ec2_keep_eip else "、Elastic IP）"))
        return
    if cluster_exists():
        run(["k3d", "cluster", "delete", CLUSTER])
        ok(f"クラスタ {CLUSTER} を削除しました")
    else:
        ok(f"クラスタ {CLUSTER} はありません")


def status(env: Env) -> None:
    """状態の表示（何も変更しない）。"""
    step("status 状態")
    if env.on_aws:
        if not succeeds(kn("get", "ns"), timeout=60):
            where = f"EKS {env.aws_cluster_name}" if env.is_aws else "EC2 3 台の k3s"
            warn(f"{where} に届きません（./lab up で作るか、aws sso login でサインインします）")
            return
    elif not cluster_exists():
        warn(f"クラスタ {CLUSTER} はありません（./lab up で作ります）")
        return
    run(kn("get", "pods", "-o", "wide"), check=False)
    run(helm("-n", NS, "list"), check=False)

    def yn(v: bool) -> str:
        return "あり" if v else "なし"

    print(f"""
環境:            {env.target}
入口:            {env.base_url}（管理画面 {env.base_url}/ui）
Google OAuth:    {yn(env.has_google())}   → SSO と Gmail の MCP
Jev の API キー: {yn(env.has_jev())}      → 一次判定（LAB_USE_JEV={env.get('LAB_USE_JEV', 'true')}）
ChatGPT:         {yn(env.has_chatgpt())}  → Codex 用のモデルと二次判定（なしなら二次判定は偽 LLM）
検証ユーザー:    {env.get('LAB_USER1_EMAIL', '（未設定）')} {env.get('LAB_USER2_EMAIL')}
クライアント:    clients/claude-lab、clients/codex-lab""")
    if http_ok(f"{env.base_url}/health/liveliness"):
        ok("LiteLLM は応答しています")
    else:
        warn("LiteLLM が応答しません")


def upgrade(env: Env, version: str) -> None:
    """LiteLLM を別の版でローリング更新する（spec 9.3 の無停止更新の試験）。
    .env の LAB_LITELLM_VERSION を書き換えるので、以後の ./lab up もこの版になる。"""
    step(f"upgrade LiteLLM を {env.litellm_version} から {version} に更新")
    if not succeeds(["docker", "manifest", "inspect", f"ghcr.io/berriai/litellm-database:{version}"], timeout=60):
        raise LabError(f"イメージ ghcr.io/berriai/litellm-database:{version} が見つかりません")
    env.set("LAB_LITELLM_VERSION", version)
    stages.images(env)
    stages.litellm(env)
    ok(f"{version} に更新しました")


def upgrade_k8s(env: Env, version: str, measure: bool = False) -> dict:
    """aws-ec2: k3s（Kubernetes）の版を 1 台ずつ上げる（変更設計書 2026-10-26-k3s版上げ 4 章）。
    順: etcd のスナップショット → 台ごとに「転送先を別の台へ → cordon と drain → インストールスクリプトの再実行 →
    Ready と版を待つ → uncordon → DB と Redis と LiteLLM がそろうのを待つ」→ SSM の版のパラメータを更新。
    Elastic IP を持つ台は最後。マイナー版は 1 つずつ（Kubernetes の版の差の規則）。
    measure=True なら偽 LLM への要求を流し続け、失敗の数と時間を報告する（test-failover と同じ測り方）。
    途中で止まっても、もう一度流せば上げ終わった台を飛ばして続きから進む。"""
    import time as _time
    from datetime import datetime, timedelta

    from . import cfn, ec2, failover
    step(f"upgrade-k8s k3s を {version} に上げる（1 台ずつ）")
    if not env.is_aws_ec2:
        raise LabError("k3s の版上げの手順は LAB_TARGET=aws-ec2 だけです（Mac の k3d は K3S_IMAGE を変えて ./lab rebuild、"
                       "EKS 版は cdk の kubernetes_version を変えて ./lab cluster）")
    target = ec2.normalize_k3s_version(version)
    out = ec2.load_outputs(env)
    ec2.ensure_forward(env)
    versions = ec2.node_versions()
    if len(versions) != cfn.NODE_COUNT:
        raise LabError(f"ノードが {len(versions)} 台です（{cfn.NODE_COUNT} 台そろってから上げてください）")
    oldest = min(versions.values(), key=ec2.parse_k3s_version)
    ec2.check_upgrade_step(oldest, target)
    if all(v == target for v in versions.values()):
        ec2.set_version_parameter(env, target)
        ok(f"3 台とも {target} です")
        return {"version": target, "upgraded": []}
    holder = ec2.eip_holder(env, out)
    order = ec2.upgrade_order(ec2.instance_ids(out), holder, ec2.forward_target())
    failover.wait_healthy(env)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ec2.etcd_snapshot(env, order[-1], f"lab-pre-upgrade-{stamp}")
    todo = [i for i in order if versions.get(ec2.node_for_instance(env, i)) != target]
    eta = datetime.now() + timedelta(minutes=8 * len(todo))
    info(f"{len(todo)} 台を {' → '.join(todo)} の順に上げます（Elastic IP の台 {holder} は最後）。"
         f"終了見込み {eta:%H:%M} ごろ（1 台 5〜8 分）")

    restarts_before = failover.litellm_restarts()
    upgraded: list[str] = []
    t0 = _time.time()
    load = failover.Load(env) if measure else None
    if load is not None:
        load.__enter__()
        _time.sleep(10)   # 平常時の様子
    try:
        for inst in todo:
            node = ec2.node_for_instance(env, inst)
            info(f"--- {node}（{inst}）: {versions[node]} → {target}")
            ec2.ensure_forward(env, avoid=inst)
            node = ec2.node_for_instance(env, inst)
            ec2.drain_node(node)
            ec2.install_k3s(env, inst, target)
            ec2.wait_node_version(node, target)
            ec2.uncordon_node(node)
            failover.wait_healthy(env)
            succeeds(["kubectl", "--context", EC2_KUBE_CONTEXT, "-n", "kube-system", "rollout", "status", "deploy/traefik",
                      "--timeout=300s"], timeout=320)
            upgraded.append(node)
            ok(f"{node} を {target} にしました")
        if load is not None:
            _time.sleep(20)   # 最後の台の後の様子
    finally:
        if load is not None:
            load.__exit__(None, None, None)
    ec2.set_version_parameter(env, target)

    result: dict = {"version": target, "upgraded": upgraded, "seconds": round(_time.time() - t0)}
    restarts_after = failover.litellm_restarts()
    result["litellm_restart_loops"] = {p: restarts_after[p] - restarts_before.get(p, 0) for p in restarts_after
                                       if restarts_after[p] - restarts_before.get(p, 0) > 1}
    if load is not None:
        samples = [s for s in load.samples if s.start >= t0]
        fails = [s for s in samples if s.status != 200]
        result.update({"requests": len(samples), "failed": len(fails), "statuses": sorted({s.status for s in fails}),
                       "failure_window_seconds": round(max(s.start for s in fails) - min(s.start for s in fails), 1) if fails else 0.0})
        (ok if not fails else warn)(f"要求 {len(samples)} 件のうち失敗 {len(fails)} 件（状態 {result['statuses']}）、"
                                   f"失敗が散らばった幅 {result['failure_window_seconds']} 秒")
    if result["litellm_restart_loops"]:
        warn(f"LiteLLM の Pod が繰り返し再起動しました: {result['litellm_restart_loops']}")
    ok(f"k3s を {target} に上げました（{result['seconds']} 秒）。SSM の版のパラメータも更新しました")
    if target != cfn.k3s_install_version():
        warn(f"core.py の K3S_IMAGE を rancher/k3s:{target.replace('+k3s', '-k3s')} に変えてコミットしてください"
             "（変えないと、次の ./lab up が版の食い違いで止まります。Mac の k3d も同じ版になります）")
    return result


def clean_all(assume_yes: bool) -> None:
    """クラスタ、生成したクライアント設定、ビルドした LiteLLM イメージを消す。
    残すもの: .env、ChatGPT のトークン、イメージの置き場（Docker Hub のキャッシュを含む）。"""
    step("clean-all 全削除")
    if Env.load().on_aws:
        raise LabError("LAB_TARGET=aws / aws-ec2 では clean-all は使えません。AWS 側は ./lab down が有料のものをすべて消します。"
                       "ローカルの生成物（clients/out、.build）は LAB_TARGET=local で clean-all を流してください")
    print(f"""次を削除します:
  - クラスタ {CLUSTER}（DB を含む）
  - clients/out/、.codex-lab/、.codex-lab-noreview/、.build/
  - ローカルの litellm-lab イメージ
残すもの: .env、ChatGPT のトークン、イメージの置き場""")
    if not assume_yes and input("よろしいですか？ [y/N] ").strip().lower() != "y":
        info("中止しました")
        return
    down()
    for d in (ROOT / "clients" / "out", ROOT / ".codex-lab", ROOT / ".codex-lab-noreview", BUILD_DIR):
        shutil.rmtree(d, ignore_errors=True)
    images = output(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).split()
    targets = [i for i in images if i.split(":")[0].split("/")[-1] == "litellm-lab"]
    if targets:
        run(["docker", "rmi", *targets], check=False, quiet=True)
    ok("削除しました")


def test_unit(env: Env) -> None:
    """単体テスト: ガードレール（LiteLLM のベースイメージの中で流す）と、この構築ツール。"""
    step("test-unit 単体テスト")
    info("構築ツール（labtool）")
    rc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "labtool" / "tests"),
                         "-t", str(ROOT), "-v"], cwd=ROOT).returncode
    if rc:
        raise LabError("構築ツールの単体テストが失敗しました")
    info("ガードレール（LiteLLM のイメージの中）")
    run(["docker", "run", "--rm", "-v", f"{ROOT / 'litellm' / 'guardrails'}:/g:ro", "-w", "/g",
         "--entrypoint", "python", env.litellm_base_image, "-m", "unittest", "discover", "-s", "tests", "-v"])
    ok("単体テストがすべて通りました")

