"""構築の段階以外の操作: 削除、状態の表示、更新。"""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import stages
from .core import (
    BUILD_DIR, CLUSTER, NS, ROOT, Env, LabError, cluster_exists, helm, http_ok,
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
    if cluster_exists():
        run(["k3d", "cluster", "delete", CLUSTER])
        ok(f"クラスタ {CLUSTER} を削除しました")
    else:
        ok(f"クラスタ {CLUSTER} はありません")


def status(env: Env) -> None:
    """状態の表示（何も変更しない）。"""
    step("status 状態")
    if env.is_aws:
        if not succeeds(kn("get", "ns"), timeout=60):
            warn(f"EKS {env.aws_cluster_name} に届きません（./lab up で作るか、aws sso login でサインインします）")
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


def clean_all(assume_yes: bool) -> None:
    """クラスタ、生成したクライアント設定、ビルドした LiteLLM イメージを消す。
    残すもの: .env、ChatGPT のトークン、イメージの置き場（Docker Hub のキャッシュを含む）。"""
    step("clean-all 全削除")
    if Env.load().is_aws:
        raise LabError("LAB_TARGET=aws では clean-all は使えません。AWS 側は ./lab down が有料のものをすべて消します。"
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

