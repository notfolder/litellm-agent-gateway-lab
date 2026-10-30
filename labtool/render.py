""".env の内容から設定を作る、副作用のない関数（単体テストの対象）。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from .core import CHATGPT_TOKEN_IN_NODE, ROOT, Env

FAKE_LLM_BASE = "http://fake-llm.litellm-lab.svc.cluster.local:8000/v1"
WORKSPACE_MCP_URL = "http://workspace-mcp.litellm-lab.svc.cluster.local:8000/mcp"
DEFAULT_S3_ENDPOINT = "http://rustfs:9000"
DEFAULT_ISOLATED_POLICY = "litellm/guardrails/policies/confidential.md"
SENTINEL_NODES = json.dumps([[f"valkey-sentinel-{i}.valkey-sentinel-headless.litellm-lab.svc.cluster.local", 26379]
                             for i in range(3)])
GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
]


# ChatGPT のモデルの「同等の API の公開価格」（1 百万トークンあたりの USD: 入力、出力、キャッシュの読み取り）。
# 契約経由の利用の概算（A）に使う（変更仕様書 6.7 節）。OpenAI の 2026-07-30 の値下げの後の値（2026-10-12 に確認）
PUBLIC_PRICES = {
    "gpt-5.6-terra": (2.00, 12.00, 0.20),
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
}


def price_env_prefix(model_name: str) -> str:
    """.env の単価の名前の接頭辞（例 review-judge → LAB_PRICE_REVIEW_JUDGE）。英数字以外は _ にする。"""
    return "LAB_PRICE_" + re.sub(r"[^A-Z0-9]", "_", model_name.upper())


def model_prices(env: Env, model_name: str, chatgpt_model: str) -> dict | None:
    """chatgpt/ のモデルの単価（model_info）。.env の LAB_PRICE_<名前>_IN / _OUT / _CACHE_READ が優先。"""
    prefix = price_env_prefix(model_name)
    default = PUBLIC_PRICES.get(chatgpt_model)
    values = []
    for i, suffix in enumerate(("_IN", "_OUT", "_CACHE_READ")):
        v = env.get(prefix + suffix) or (str(default[i]) if default else "")
        if not v:
            return None
        values.append(float(v) / 1_000_000)
    return {"input_cost_per_token": values[0], "output_cost_per_token": values[1],
            "cache_read_input_token_cost": values[2]}


def _fake_params() -> dict:
    return {"model": "openai/fake-tools", "api_key": "sk-fake", "api_base": FAKE_LLM_BASE}


def litellm_config(env: Env, has_chatgpt: bool) -> dict:
    """LiteLLM のプロキシ設定（config.yaml）。

    手作業の準備が済んでいない部分は含めない（docs/design.md 6.3）。
    - Google の OAuth クライアントが無い → Gmail の MCP を含めない
    - TypeSafe の API キーが無い → 一次判定（Jev）を無効にする
    - ChatGPT のトークンが無い → Codex 用のモデルを含めない。chatgpt/ のモデルをトークン無しで
      設定すると、LiteLLM が起動の途中でデバイスコードのログインを始めて止まるため（2026-10-11 に確認）。
      二次判定は試験用の偽 LLM（fake-llm）で代用する
    """
    codex_model = env.get("LAB_CODEX_MODEL", "gpt-5.6-terra")
    judge_model = env.get("LAB_JUDGE_MODEL", "gpt-5.6-luna")
    # クラウドの環境では Jev を使わない（セルフホスト版が無く、ツールコールの内容が社外に出るため。変更仕様書 0.2 節）
    use_jev = env.has_jev() and env.get("LAB_USE_JEV", "true").lower() == "true" and not env.on_cloud

    if env.is_aws:
        # EKS 版: Claude Code は Bedrock の Claude（Claude Max のトークン転送は使わない）
        model_list: list[dict] = [
            {"model_name": name, "litellm_params": bedrock_params(env, model_id)}
            for name, model_id in aws_claude_models(env).items()
        ]
    else:
        # Mac と VM の環境: Claude Code（Claude Max のトークンを転送する。api_key は書かない。
        # 変更仕様書 2026-10-30-VM環境の共通化とGCP追加 4 章）
        model_list = [{"model_name": "anthropic/*", "litellm_params": {"model": "anthropic/*"}}]
    if has_chatgpt:
        # Codex（ChatGPT Plus）。単価は概算用（ChatGPT の定額の契約は単価表に無く、そのままでは 0 になるため）
        entry = {"model_name": codex_model, "litellm_params": {"model": f"chatgpt/{codex_model}"}}
        if prices := model_prices(env, codex_model, codex_model):
            entry["model_info"] = prices
        model_list.append(entry)
    # 審査の試験用の偽 LLM（tests/fake-llm）。ツールコールを返す
    # 試験用の単価（1 百万トークンあたり 1 / 2 USD）。精算の按分を試すには、概算（A）が 0 でないことが要るため
    model_list.append({"model_name": "fake-tools", "litellm_params": _fake_params(),
                       "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}})
    # 二次判定（審査官）。Mac は ChatGPT Plus のモデル、EKS 版は Bedrock の Claude Haiku 4.5（日本内で処理するプロファイル）、
    # VM の環境は Anthropic の API キーで Claude Haiku 4.5（キーの環境変数は ANTHROPIC_API_KEY ではない名前。
    # Pod にその名前があると anthropic/* の転送がトークンの代わりにキーを使いうるため。変更設計書 8 章）。無ければ試験用の偽 LLM
    def judge_params(model_name: str, chatgpt_model: str) -> tuple[dict, dict | None]:
        if env.is_aws:
            return bedrock_params(env, aws_judge_model(env)), None
        if env.is_vm:
            return {"model": f"anthropic/{vm_judge_model(env)}", "api_key": f"os.environ/{JUDGE_KEY_ENV}"}, None
        if has_chatgpt:
            return {"model": f"chatgpt/{chatgpt_model}"}, model_prices(env, model_name, chatgpt_model)
        return _fake_params(), None

    params, prices = judge_params("review-judge", judge_model)
    judge: dict = {"model_name": "review-judge", "litellm_params": params}
    if prices:
        judge["model_info"] = prices
    model_list.append(judge)
    # 文脈独立審査（機密情報の持ち出し）の審査官。会話を見ずに、ツール名・引数・ツールの定義だけで判定する
    isolated = isolated_review_enabled(env)
    if isolated:
        isolated_model = env.get("LAB_ISOLATED_JUDGE_MODEL") or judge_model
        params, prices = judge_params("isolated-judge", isolated_model)
        entry = {"model_name": "isolated-judge", "litellm_params": params}
        if prices:
            entry["model_info"] = prices
        model_list.append(entry)

    config: dict = {
        "model_list": model_list,
        # 監査ログ（litellm/guardrails/audit_log.py。変更設計書 5 章）
        "litellm_settings": {"callbacks": ["audit_log.audit_logger_instance"]},
        "guardrails": [{
            "guardrail_name": "tool-call-review",
            "litellm_params": {
                "guardrail": "tool_call_review.ToolCallReview",
                # 中身の審査が入りなら、要求に方針を足す pre_call も使う（変更設計書 3.9 節）
                "mode": ["pre_call", "post_call"] if isolated_content_enabled(env) else ["post_call"],
                "default_on": True,
                "use_jev": use_jev,
                "jev_model": "typesafe/jev-latest",
                "jev_threshold": float(env.get("LAB_JEV_THRESHOLD", "0.2")),
                "judge_model": "review-judge",
                "fail_open": False,
                # 疎通確認（偽 LLM の fake-tools）は、結果が決まるように偽 LLM の審査官で判定する
                "test_model": "fake-tools",
                "test_judge_model": "fake-tools",
                # 無効のときも必ず出す（ガードレールの既定は true で、判定基準が空だと起動しなくなるため）
                "isolated_review": isolated,
                # Claude Code にも文脈あり審査を掛けるか（既定は切り。auto mode を外した運用の守り）
                "context_review_claude_code": context_review_claude_code(env),
            },
        }],
    }
    if isolated:
        config["guardrails"][0]["litellm_params"].update({
            "isolated_review_claude_code": env.get("LAB_ISOLATED_CLAUDE_CODE", "true").lower() == "true",
            "isolated_judge_model": "isolated-judge",
            "isolated_policy": isolated_policy(env),
            "isolated_fail_open": False,
            "test_isolated_judge_model": "fake-tools",
            # 送信するファイルの中身の審査（.history/2026-10-13-送信するファイルの中身の審査/）
            "isolated_content": isolated_content_enabled(env),
            "isolated_content_max_bytes": content_max_bytes(env),
            "isolated_content_max_files": content_max_files(env),
            # 送信の宛先の許可リスト（変更仕様書 7.4 節）
            "isolated_destinations": isolated_destinations(env),
        })

    if env.has_google():
        config["mcp_servers"] = {"gmail": {
            "url": WORKSPACE_MCP_URL,
            "transport": "http",
            "auth_type": "oauth2",
            "oauth2_flow": "authorization_code",
            "client_id": "os.environ/GOOGLE_OAUTH_CLIENT_ID",
            "client_secret": "os.environ/GOOGLE_OAUTH_CLIENT_SECRET",
            "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": GMAIL_SCOPES,
            # Gmail にサインインしたアカウントと LiteLLM のユーザーのメールが違えば拒否する
            "oauth_identity_binding": {
                "mode": "enforce",
                "issuer": "https://accounts.google.com",
                "audiences": [env.get("GOOGLE_OAUTH_CLIENT_ID")],
                "principal_claim": "email",
                "caller_field": "user_email",
                "require_email_verified": True,
            },
        }}

    # Redis（Valkey）は環境変数の Sentinel の指定（REDIS_SENTINEL_NODES など）だけで使う。router_settings に
    # Redis のキー（redis_host など）を書くと、ルーターが Sentinel を使わない別の接続を作る（変更設計書 4.5 節）
    config["router_settings"] = {
        "routing_strategy": "simple-shuffle",
        # Claude Max のトークン切れの 1 人のせいで、他の人まで止まらないように
        "disable_cooldowns": True,
    }
    config["general_settings"] = {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "database_url": "os.environ/DATABASE_URL",
        # Pod ごとの接続数の上限（変更設計書 3.4 節）。Pooler 経由なら 10、直結なら 5（最大 4 Pod で 20 本。
        # PostgreSQL の max_connections 200 に収まる）
        "database_connection_pool_limit": 10 if db_pooler(env) == "pooler" else 5,
        "proxy_batch_write_at": 60,
        # 利用量の DB への書き込みを Redis に溜め、1 台がまとめて書く（10 台以上で公式が推奨。本番は 20〜30 Pod）
        "use_redis_transaction_buffer": True,
        "require_key_mcp_access_defined": True,
        # 利用ログ（LiteLLM_SpendLogs）は書かない。利用量と利用料は監査ログから集計する。使用額と日次の集計は
        # この設定でも更新され続ける（v1.104.2 のソースで確認。変更仕様書 6.6 節）
        "disable_spend_logs": True,
    }
    return config


def context_review_claude_code(env: Env) -> bool:
    """Claude Code にも文脈あり審査を掛けるか（LAB_CONTEXT_REVIEW_CLAUDE_CODE。既定は false）。"""
    return env.get("LAB_CONTEXT_REVIEW_CLAUDE_CODE", "false").lower() == "true"


def isolated_review_enabled(env: Env) -> bool:
    return env.get("LAB_ISOLATED_REVIEW", "true").lower() == "true"


def isolated_content_enabled(env: Env) -> bool:
    """送信するファイルの中身の審査を使うか（文脈独立審査が有効なときだけ効く）。"""
    return isolated_review_enabled(env) and env.get("LAB_ISOLATED_CONTENT", "true").lower() == "true"


def content_max_bytes(env: Env) -> int:
    return int(env.get("LAB_ISOLATED_CONTENT_MAX_BYTES", "24576"))


def content_max_files(env: Env) -> int:
    return int(env.get("LAB_ISOLATED_CONTENT_MAX_FILES", "5"))


def isolated_destinations(env: Env) -> list[str]:
    """送信の宛先の許可リスト（LAB_ISOLATED_DESTINATIONS。カンマ区切り。既定は private）。"""
    return [s.strip() for s in env.get("LAB_ISOLATED_DESTINATIONS", "private").split(",") if s.strip()]


def noreview_enabled(env: Env) -> bool:
    """審査を外したキー（ユーザー 1 の検証用）を作るか。既定は false。"""
    return env.get("LAB_NOREVIEW_KEYS", "false").lower() == "true"


def noreview_keys(env: Env) -> tuple[str | None, str | None]:
    """審査を外したキー（Claude Code 用、Codex 用）。固定値は .env に生成される。"""
    return env.get("LAB_KEY_USER1_CLAUDE_NOREVIEW"), env.get("LAB_KEY_USER1_CODEX_NOREVIEW")


def isolated_policy(env: Env) -> str:
    """文脈独立審査の判定基準の文章（LAB_ISOLATED_POLICY_FILE。相対パスはリポジトリから）。空ならエラー。"""
    path = Path(env.get("LAB_ISOLATED_POLICY_FILE") or DEFAULT_ISOLATED_POLICY)
    if not path.is_absolute():
        path = ROOT / path
    text = path.read_text().strip()
    if not text:
        raise ValueError(f"判定基準のファイルが空です: {path}")
    return text


def db_pooler(env: Env) -> str:
    """LiteLLM の DB の接続先: pooler（PgBouncer。既定）か direct（primary に直結）。"""
    mode = env.get("LAB_DB_POOLER", "pooler")
    if mode not in ("pooler", "direct"):
        raise ValueError(f"LAB_DB_POOLER は pooler か direct です（{mode}）")
    return mode


def to_yaml(data: dict, header: str = "") -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return (f"# {header}\n" if header else "") + body


def helm_overrides(env: Env, image_repo: str, image_tag: str, config_hash: str, aws_out: dict | None = None) -> dict:
    """values.yaml に重ねる、環境ごとの値。aws では CloudFormation の出力（aws_out）から接続先を入れる
    （変更仕様書 2026-10-24-AWS構成 2.3 節。差分はここに閉じ込め、values.yaml の骨格は共通）。
    VM の環境（aws-ec2 / gcp-vm）は Kubernetes の中が Mac と同じなので local の値を使い、監査ログのバケットとイメージの取り方だけ変える。"""
    pooler = db_pooler(env) == "pooler" and not env.is_aws   # EKS 版は直結のみ（変更仕様書 3.1 節）
    env_vars = {
        "LITELLM_MODE": "PRODUCTION",
        "PROXY_BASE_URL": env.base_url,
        "CHATGPT_TOKEN_DIR": CHATGPT_TOKEN_IN_NODE,
        "UI_USERNAME": "admin",
        "AUDIT_FLUSH_SECONDS": env.get("LAB_AUDIT_FLUSH_SECONDS", "10"),
        # DB 移行は自前の Job で流すので、Pod の起動時には流さない（チャートは移行の Job を使うときしか入れない）
        "DISABLE_SCHEMA_UPDATE": "true",
    }
    if pooler:
        # PgBouncer の transaction モードでは準備済みの問い合わせを使わない（LiteLLM が URL に pgbouncer=true を足す）
        env_vars["DATABASE_DISABLE_PREPARED_STATEMENTS"] = "true"
    overrides: dict = {
        "image": {"repository": image_repo, "tag": image_tag},
        # 設定のハッシュを Pod の注釈に入れ、設定が変わったときだけ Pod を作り直す
        "podAnnotations": {"lab/config-hash": config_hash},
        "envVars": env_vars,
    }
    if not env.is_aws:
        env_vars.update({
            # Valkey は Sentinel 経由（パスワードは Secret litellm-secrets の REDIS_PASSWORD と REDIS_SENTINEL_PASSWORD）
            "REDIS_SENTINEL_NODES": SENTINEL_NODES,
            "REDIS_SERVICE_NAME": "mymaster",
            # 監査ログの書き込み先（資格情報は Secret litellm-secrets の AUDIT_S3_ACCESS_KEY / AUDIT_S3_SECRET_KEY）
            "AUDIT_S3_ENDPOINT": env.get("LAB_S3_ENDPOINT", DEFAULT_S3_ENDPOINT),
            "AUDIT_S3_REGION": env.get("LAB_S3_REGION", "us-east-1"),
            "AUDIT_S3_BUCKET": AUDIT_RAW,
        })
        # チャートは db.useExisting のとき、DATABASE_URL を Pod の env に直接書く（接続先はここで切り替える）
        overrides["db"] = {"endpoint": "litellm-pg-pooler-rw" if pooler else "litellm-pg-rw"}
        if env.is_vm:
            env_vars.update({
                # 監査ログはクラウドのバケット（接続先はクラウド固有層の s3_endpoint。AWS は空＝既定、GCP は storage.googleapis.com。
                # 資格情報は静的キー。接頭辞付きの名前）
                "AUDIT_S3_ENDPOINT": vm_s3_endpoint(env),
                "AUDIT_S3_REGION": env.vm_region,
                "AUDIT_S3_BUCKET": bucket_name(env, AUDIT_RAW),
            })
            # イメージはクラスタの中の置き場（registry）から。chatgpt/ のトークンは置かない（共有ファイルシステムが無い。
            # 変更仕様書 2026-10-25-EC2三台構成 4.2 節）ので hostPath を外し、退避先だけにする
            overrides["volumes"] = [{"name": "audit-spool", "emptyDir": {"sizeLimit": "1Gi"}}]
            overrides["volumeMounts"] = [{"name": "audit-spool", "mountPath": "/var/lib/audit-spool"}]
        return overrides

    out = aws_out or {}
    env_vars.update({
        # ElastiCache for Valkey の primary エンドポイント（TLS、認証トークンは Secret litellm-secrets の REDIS_PASSWORD）
        "REDIS_HOST": out.get("ValkeyPrimaryEndpoint", ""),
        "REDIS_PORT": out.get("ValkeyPort", "6379"),
        "REDIS_SSL": "True",
        # 監査ログは AWS S3 に Pod Identity で書く（静的キー無し。接続先は空で既定のエンドポイント）
        "AUDIT_S3_ENDPOINT": "",
        "AUDIT_S3_REGION": env.aws_region,
        "AUDIT_S3_BUCKET": bucket_name(env, AUDIT_RAW),
        "AUDIT_S3_CREDENTIAL_CHAIN": "true",
        # Bedrock の呼び出し（boto3 の既定のリージョンと Pod Identity の資格情報）
        "AWS_REGION": env.aws_region,
        "AWS_DEFAULT_REGION": env.aws_region,
    })
    overrides.update({
        # Pod Identity で IAM ロールを結び付ける ServiceAccount（名前は labtool/aws.py の POD_IDENTITIES と同じ）
        "serviceAccount": {"create": True, "name": "litellm"},
        # Aurora の writer に直結。Secret litellm-pg-app は External Secrets Operator が Secrets Manager から作る。
        # Aurora は TLS を要求するので sslmode=require（チャートの既定の URL には無い）
        "db": {"endpoint": out.get("AuroraWriterEndpoint", ""),
               "url": "postgresql://$(DATABASE_USERNAME):$(DATABASE_PASSWORD)@$(DATABASE_HOST):5432/$(DATABASE_NAME)"
                      "?sslmode=require"},
        # ALB のヘルスチェックの経路（Service の注釈。Ingress 全体の既定は / なので Service ごとに指定）
        "service": {"annotations": {"alb.ingress.kubernetes.io/healthcheck-path": "/health/liveliness"}},
        # chatgpt/ のトークンは全 Pod で共有する EFS の PVC（Mac は hostPath。変更仕様書 4.2 節）
        "volumes": [
            {"name": "chatgpt-token", "persistentVolumeClaim": {"claimName": "chatgpt-token"}},
            {"name": "audit-spool", "emptyDir": {"sizeLimit": "1Gi"}},
        ],
    })
    return overrides


def image_tag(base_image: str, context_dir: Path) -> str:
    """LiteLLM イメージのタグ: ベースイメージと、Dockerfile・ガードレールの中身のハッシュ。"""
    h = hashlib.sha256(base_image.encode())
    files = [context_dir / "Dockerfile"] + sorted(
        p for p in (context_dir / "guardrails").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for p in files:
        h.update(str(p.relative_to(context_dir)).encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:12]


def secrets_for_litellm(env: Env) -> dict[str, str]:
    data = {
        "LITELLM_MASTER_KEY": env.get("LITELLM_MASTER_KEY"),
        "LITELLM_SALT_KEY": env.get("LITELLM_SALT_KEY"),
        "REDIS_PASSWORD": env.get("REDIS_PASSWORD"),
        "REDIS_SENTINEL_PASSWORD": env.get("SENTINEL_PASSWORD"),
        "AUDIT_S3_ACCESS_KEY": env.get(s3_env_names("litellm-writer")[0]),
        "AUDIT_S3_SECRET_KEY": env.get(s3_env_names("litellm-writer")[1]),
    }
    if env.has_google():
        # 管理画面の Google SSO（GOOGLE_*）と Gmail の MCP（GOOGLE_OAUTH_*）で同じクライアントを使う
        cid, secret = env.get("GOOGLE_OAUTH_CLIENT_ID"), env.get("GOOGLE_OAUTH_CLIENT_SECRET")
        data |= {"GOOGLE_CLIENT_ID": cid, "GOOGLE_CLIENT_SECRET": secret,
                 "GOOGLE_OAUTH_CLIENT_ID": cid, "GOOGLE_OAUTH_CLIENT_SECRET": secret}
    if env.has_jev():
        data["TYPESAFE_API_KEY"] = env.get("TYPESAFE_API_KEY")
    if env.is_vm:
        # 審査官の Anthropic の API キー。ANTHROPIC_API_KEY の名前は使わない（Pod にあると anthropic/* の転送が
        # Max のトークンの代わりにキーを使いうる。変更設計書 2026-10-30-VM環境の共通化とGCP追加 8 章）
        data[JUDGE_KEY_ENV] = env.get(JUDGE_KEY_ENV)
    return data


def client_keys(env: Env, user: str) -> tuple[str, str]:
    """クライアントが使うキー（Claude Code 用、Codex 用）。ユーザーが未設定ならマスターキー。"""
    if not env.has_users():
        master = env.get("LITELLM_MASTER_KEY")
        return master, master
    return env.get(f"LAB_KEY_USER{user}_CLAUDE"), env.get(f"LAB_KEY_USER{user}_CODEX")


def client_files(env: Env, claude_key: str, codex_key: str, codex_home: Path,
                 noreview_codex_home: Path) -> dict[str, str]:
    """Claude Code と Codex の検証用の設定。キーは clients/out/ からの相対パス、値は中身。
    Codex の config.toml は "codex-config.toml"（審査なしは "noreview-codex-config.toml"）で返す。"""
    base = env.base_url
    files: dict[str, str] = {}
    files["claude.env"] = (
        "# clients/claude-lab が読み込む（生成物。手で編集しない）\n"
        f"export ANTHROPIC_BASE_URL='{base}'\n"
        f"export ANTHROPIC_CUSTOM_HEADERS='x-litellm-api-key: Bearer {claude_key}'\n"
        f"export LAB_LITELLM_KEY='{claude_key}'\n"
    )
    if env.is_aws:
        # EKS 版: Bedrock のモデルは名前を固定で登録するので、Claude Code の別名（sonnet / haiku / opus）をその名前に向ける
        for var, name in claude_code_model_env(env).items():
            files["claude.env"] += f"export {var}='{name}'\n"
    host = env.public_host
    files["claude-settings.json"] = json.dumps({"autoMode": {"environment": [
        "$defaults",
        f"Trusted internal domains: {host}（検証用の LLM ゲートウェイ。Gmail の MCP もここ経由）",
    ]}}, ensure_ascii=False, indent=2) + "\n"
    mcp = {"mcpServers": {}}
    if env.has_google():
        mcp["mcpServers"]["gmail"] = {"type": "http", "url": f"{base}/gmail/mcp",
                                      "headers": {"x-litellm-api-key": f"Bearer {claude_key}"}}
    files["mcp.json"] = json.dumps(mcp, indent=2) + "\n"

    codex_model = env.get("LAB_CODEX_MODEL", "gpt-5.6-terra")
    toml = (
        "# 生成物（./lab clients）。手で編集しない\n"
        f'model = "{codex_model}"\n'
        'model_provider = "litellm"\n'
        "# 任意の MCP サーバー（gmail）のツールの一覧を、最初のターンの前に最大 10 秒待つ。Codex は既定では\n"
        "# ほとんど待たずにモデルを呼ぶので、ゲートウェイの tools/list（約 0.6〜1 秒）が間に合わないと、\n"
        "# その回は MCP のツールを使えない（2026-10-11 に確認）。required = true でも待つが、その場合は\n"
        "# gmail のサインインが切れると Codex 自体が起動しなくなる\n"
        "mcp_optional_startup_grace_ms = 10000\n"
        "\n"
        "[model_providers.litellm]\n"
        'name = "LiteLLM (lab)"\n'
        f'base_url = "{base}/v1"\n'
        'env_key = "LITELLM_API_KEY"\n'
        'wire_api = "responses"\n'
    )
    if env.has_google():
        toml += (
            "\n[mcp_servers.gmail]\n"
            f'url = "{base}/gmail/mcp"\n'
            'env_http_headers = { "x-litellm-api-key" = "LITELLM_MCP_AUTH" }\n'
            "tool_timeout_sec = 180\n"
        )
    files["codex-config.toml"] = toml
    files["codex.env"] = (
        "# clients/codex-lab が読み込む（生成物。手で編集しない）\n"
        f"export CODEX_HOME='{codex_home}'\n"
        f"export LITELLM_API_KEY='{codex_key}'\n"
        f"export LITELLM_MCP_AUTH='Bearer {codex_key}'\n"
    )
    # 審査を外したキー（LAB_NOREVIEW_KEYS=true のときだけ。検証用。docs/manual-check.md）
    if noreview_enabled(env):
        nr_claude, nr_codex = noreview_keys(env)
        if nr_codex:
            files["noreview-codex.env"] = (
                "# clients/codex-lab-noreview が読み込む（審査を外したキー。生成物）\n"
                f"export CODEX_HOME='{noreview_codex_home}'\n"
                f"export LITELLM_API_KEY='{nr_codex}'\n"
                f"export LITELLM_MCP_AUTH='Bearer {nr_codex}'\n"
            )
            # CODEX_HOME を分ける。Codex は CODEX_HOME ごとに常駐プロセス（app-server）を立て、起動時のキーを
            # 持ち続けるので、共有すると審査ありの codex-lab が審査なしのキーで送ることがある（2026-10-13 に確認）
            files["noreview-codex-config.toml"] = toml
        if nr_claude:
            files["noreview-claude.env"] = (
                "# clients/claude-lab-noreview が読み込む（審査を外したキー。生成物）\n"
                f"export ANTHROPIC_BASE_URL='{base}'\n"
                f"export ANTHROPIC_CUSTOM_HEADERS='x-litellm-api-key: Bearer {nr_claude}'\n"
                f"export LAB_LITELLM_KEY='{nr_claude}'\n"
            )
            nmcp = {"mcpServers": {}}
            if env.has_google():
                nmcp["mcpServers"]["gmail"] = {"type": "http", "url": f"{base}/gmail/mcp",
                                               "headers": {"x-litellm-api-key": f"Bearer {nr_claude}"}}
            files["noreview-mcp.json"] = json.dumps(nmcp, indent=2) + "\n"
    return files


# ---- オブジェクトストレージ（変更設計書 2 章） ----
AUDIT_RAW, AUDIT_ACCESS, AUDIT_DERIVED, PG_BACKUP = "audit-raw", "audit-access", "audit-derived", "pg-backup"
S3_USERS = ["litellm-writer", "audit-compactor", "audit-viewer", "pg-backup"]


def s3_env_names(user: str) -> tuple[str, str]:
    """利用者のアクセスキーとシークレットを置く .env の名前（例 S3_LITELLM_WRITER_KEY / _SECRET）。"""
    base = "S3_" + user.upper().replace("-", "_")
    return base + "_KEY", base + "_SECRET"


def _lifecycle(rule_id: str, prefix: str, days: int | None, noncurrent_days: int | None) -> dict:
    rule: dict = {"ID": rule_id, "Status": "Enabled", "Filter": {"Prefix": prefix}}
    if days is not None:
        rule["Expiration"] = {"Days": days}
    if noncurrent_days is not None:
        rule["NoncurrentVersionExpiration"] = {"NoncurrentDays": noncurrent_days}
    return rule


def s3_buckets(env: Env) -> dict[str, dict]:
    """バケットごとの設定。lock は Object Lock の既定の保持（None なら Object Lock を使わない）。
    保持の日数 + 1 日で期限切れにする（保持が切れた版から消える）。"""
    mode = env.get("LAB_AUDIT_LOCK_MODE", "GOVERNANCE")
    raw_days = int(env.get("LAB_AUDIT_RETENTION_DAYS", "90"))
    access_days = int(env.get("LAB_AUDIT_ACCESS_RETENTION_DAYS", "400"))
    return {
        AUDIT_RAW: {"lock": {"Mode": mode, "Days": raw_days}, "versioning": True,
                    "lifecycle": [_lifecycle("all", "", raw_days + 1, raw_days + 1)]},
        AUDIT_ACCESS: {"lock": {"Mode": mode, "Days": access_days}, "versioning": True,
                       "lifecycle": [_lifecycle("all", "", access_days + 1, access_days + 1)]},
        # 請求の単位と換算のレート（billing/contracts/、billing/fx_rates/）は、編集のたびに新しいファイルを足し、
        # 最新のファイルを今の値とする。小さく編集も稀なので消さない
        AUDIT_DERIVED: {"lock": None, "versioning": True, "lifecycle": [
            _lifecycle("meta", "meta/", raw_days + 1, 1),
            _lifecycle("meta-day", "meta_day/", raw_days + 1, 1),
            _lifecycle("usage-daily", "usage_daily/", 400, 1),
            _lifecycle("settlement", "billing/settlement/", 400, 400),
            _lifecycle("synth", "synth/", 7, 1),
        ]},
        PG_BACKUP: {"lock": None, "versioning": False, "lifecycle": []},
    }


def _stmt(actions: list[str], resources: list[str]) -> dict:
    return {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{r}" for r in resources]}


def s3_policies() -> dict[str, dict]:
    """利用者ごとの権限（必要な操作だけ）。どの利用者にも、原本の削除と保持の回避は許さない。"""
    def doc(*stmts: dict) -> dict:
        return {"Version": "2012-10-17", "Statement": list(stmts)}

    read = ["s3:GetObject"]
    lst = ["s3:ListBucket"]
    return {
        "litellm-writer": doc(_stmt(["s3:PutObject"], [f"{AUDIT_RAW}/raw/*"])),
        "audit-compactor": doc(
            _stmt(lst, [AUDIT_RAW, AUDIT_DERIVED]),
            _stmt(read, [f"{AUDIT_RAW}/*", f"{AUDIT_DERIVED}/*"]),
            _stmt(["s3:PutObject"], [f"{AUDIT_DERIVED}/*"]),
        ),
        "audit-viewer": doc(
            _stmt(lst, [AUDIT_RAW, AUDIT_ACCESS, AUDIT_DERIVED]),
            _stmt(read, [f"{AUDIT_RAW}/*", f"{AUDIT_ACCESS}/*", f"{AUDIT_DERIVED}/*"]),
            _stmt(["s3:PutObject"], [f"{AUDIT_ACCESS}/*", f"{AUDIT_DERIVED}/billing/contracts/*",
                                     f"{AUDIT_DERIVED}/billing/fx_rates/*"]),
        ),
        "pg-backup": doc(_stmt(["s3:*"], [PG_BACKUP, f"{PG_BACKUP}/*"])),
    }


def dir_hash(path: Path) -> str:
    """フォルダの中身のハッシュ（イメージのタグ）。__pycache__ と .pyc は含めない。"""
    h = hashlib.sha256()
    for p in sorted(x for x in path.rglob("*") if x.is_file() and "__pycache__" not in x.parts and x.suffix != ".pyc"):
        h.update(str(p.relative_to(path)).encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:12]


def audit_oidc_client(env: Env) -> tuple[str, str]:
    """検索アプリのサインインの OAuth クライアント（既定は Google の OAuth クライアントと同じ）。"""
    return (env.get("LAB_AUDIT_OIDC_CLIENT_ID") or env.get("GOOGLE_OAUTH_CLIENT_ID"),
            env.get("LAB_AUDIT_OIDC_CLIENT_SECRET") or env.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def initial_contracts(env: Env) -> dict:
    """請求の単位の初期値（.env の LAB_CONTRACTS。無ければ検証環境の ChatGPT Plus と Claude Max）。"""
    if env.get("LAB_CONTRACTS"):
        return json.loads(env.get("LAB_CONTRACTS"))
    from datetime import datetime, timedelta, timezone
    month = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m")
    codex = env.get("LAB_CODEX_MODEL", "gpt-5.6-terra")
    if env.is_aws:
        # EKS 版: Claude Code と審査官は Bedrock の従量課金（実費は月ごとに請求書から入れる）、Codex は ChatGPT Plus の定額のまま
        return {"schema": 1, "contracts": [
            {"id": "bedrock", "name": "Amazon Bedrock", "kind": "invoice", "currency": "USD",
             "models": ["claude-*", "review-judge", "isolated-judge"], "fees": {}},
            {"id": "chatgpt-plus", "name": "ChatGPT Plus", "kind": "subscription", "currency": "USD",
             "models": [codex], "fees": {month: 20.0}}]}
    if env.is_vm:
        # VM の環境: Claude Code は Mac と同じ Claude Max の定額、審査官は Anthropic の API の従量（chatgpt/ は置かない）
        return {"schema": 1, "contracts": [
            {"id": "claude-max", "name": "Claude Max", "kind": "subscription", "currency": "USD",
             "models": ["anthropic/*", "claude-*"], "fees": {month: 100.0}},
            {"id": "anthropic-api", "name": "Anthropic API", "kind": "invoice", "currency": "USD",
             "models": ["review-judge", "isolated-judge"], "fees": {}}]}
    return {"schema": 1, "contracts": [
        {"id": "chatgpt-plus", "name": "ChatGPT Plus", "kind": "subscription", "currency": "USD",
         "models": [codex, "review-judge", "isolated-judge"], "fees": {month: 20.0}},
        {"id": "claude-max", "name": "Claude Max", "kind": "subscription", "currency": "USD",
         "models": ["anthropic/*", "claude-*"], "fees": {month: 100.0}},
    ]}


def initial_fx_rates(env: Env) -> dict:
    return json.loads(env.get("LAB_FX_RATES")) if env.get("LAB_FX_RATES") else {"schema": 1, "usd_jpy": {}}


def pin_deployments(manifest_text: str, node_selector: dict) -> str:
    """マニフェストの中の Deployment に nodeSelector を足す（取得した公式のマニフェストを、server ノードに置くため）。"""
    docs = [d for d in yaml.safe_load_all(manifest_text) if d]
    for d in docs:
        if d.get("kind") == "Deployment":
            d["spec"]["template"]["spec"]["nodeSelector"] = node_selector
    return yaml.safe_dump_all(docs, sort_keys=False)


# ---- AWS 環境（LAB_TARGET=aws）。変更仕様書 2026-10-24-AWS構成 ----
# Bedrock のモデル ID の既定（日本内で処理するクロスリージョン推論プロファイル。.env の LAB_BEDROCK_* で変えられる）
BEDROCK_DEFAULTS = {
    "claude-sonnet-4-5": ("LAB_BEDROCK_SONNET", "jp.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    "claude-haiku-4-5": ("LAB_BEDROCK_HAIKU", "jp.anthropic.claude-haiku-4-5-20251001-v1:0"),
    # Opus は日本内のプロファイルが無いので既定は空（登録しない）。使うなら LAB_BEDROCK_OPUS に Global の ID を入れる
    "claude-opus-4-6": ("LAB_BEDROCK_OPUS", ""),
}
# LiteLLM のパススルー（素通し）の経路。入口で 403 にする（spec 4.2。k8s/ingress.yaml の Traefik の正規表現と同じ一覧）
PASSTHROUGH_PROVIDERS = [
    "anthropic", "assemblyai", "eu.assemblyai", "azure", "azure_ai", "bedrock", "cohere", "cursor", "fal_ai", "gemini",
    "gigachat", "langfuse", "milvus", "mistral", "nvidia_nim", "openai", "openai_passthrough", "openrouter",
    "tinyfish", "typesafe", "vertex_ai", "vertex-ai", "vllm", "watsonx",
]
ESO_NAMESPACE = "external-secrets"
ESO_STORE = "aws-secretsmanager"


def bucket_prefix(env: Env, account: str) -> str:
    """バケット名の接頭辞。クラウドでは全世界で一意にするため <接頭辞>-<アカウント / プロジェクト>-。Mac（RustFS）は空。
    VM の環境の接頭辞はクラウド固有層（clouds.*.bucket_prefix）が決め、preflight が LAB_BUCKET_PREFIX に載せる。"""
    if env.is_vm:
        return env.get("LAB_BUCKET_PREFIX", "")
    return f"{env.aws_prefix}-{account}-" if env.is_aws else ""


def bucket_name(env: Env, logical: str) -> str:
    """論理名（audit-raw など）から実際のバケット名。クラウドでは接頭辞（preflight が Env の LAB_BUCKET_PREFIX に載せる。
    EKS 版の LAB_AWS_BUCKET_PREFIX も読む）を付ける。"""
    if not env.on_cloud:
        return logical
    return (env.get("LAB_BUCKET_PREFIX") or env.get("LAB_AWS_BUCKET_PREFIX", "")) + logical


def bedrock_params(env: Env, model_id: str) -> dict:
    """Bedrock のモデルの litellm_params（EKS 版。Pod Identity の資格情報チェーン）。"""
    return {"model": f"bedrock/{model_id}", "aws_region_name": env.aws_region}


# VM の環境（aws-ec2 / gcp-vm）の審査官: Anthropic の API キーで Claude Haiku 4.5（変更仕様書 2026-10-30 4 章）
JUDGE_KEY_ENV = "LAB_JUDGE_ANTHROPIC_API_KEY"
DEFAULT_VM_JUDGE_MODEL = "claude-haiku-4-5-20251001"


def vm_judge_model(env: Env) -> str:
    return env.get("LAB_VM_JUDGE_MODEL") or DEFAULT_VM_JUDGE_MODEL


def vm_s3_endpoint(env: Env) -> str:
    """クラスタの中の S3 互換の設定に渡す接続先（AWS は空＝既定、GCP は storage.googleapis.com）。"""
    from . import clouds
    return clouds.for_env(env).s3_endpoint(env)


def aws_claude_models(env: Env) -> dict[str, str]:
    """Claude Code 用に登録するモデル名と Bedrock のモデル ID（空の ID は登録しない）。"""
    return {name: env.get(var) or default for name, (var, default) in BEDROCK_DEFAULTS.items()
            if env.get(var) or default}


def aws_judge_model(env: Env) -> str:
    """2 つの審査官の Bedrock のモデル ID（既定は Claude Haiku 4.5 の日本内プロファイル）。"""
    return env.get("LAB_AWS_JUDGE_MODEL") or BEDROCK_DEFAULTS["claude-haiku-4-5"][1]


def claude_code_model_env(env: Env) -> dict[str, str]:
    """Claude Code の別名をゲートウェイのモデル名に向ける環境変数。Opus が無ければ Sonnet に落とす。"""
    models = aws_claude_models(env)
    sonnet = "claude-sonnet-4-5" if "claude-sonnet-4-5" in models else next(iter(models))
    haiku = "claude-haiku-4-5" if "claude-haiku-4-5" in models else sonnet
    opus = "claude-opus-4-6" if "claude-opus-4-6" in models else sonnet
    return {"ANTHROPIC_MODEL": sonnet, "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku, "ANTHROPIC_DEFAULT_OPUS_MODEL": opus,
            "CLAUDE_CODE_SUBAGENT_MODEL": sonnet}


def service_accounts_manifest() -> str:
    """監査アプリの ServiceAccount（両環境で作る。aws では Pod Identity で IAM ロールが付く）。"""
    docs = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": n, "namespace": "litellm-lab"}}
            for n in ("audit-compactor", "audit-viewer")]
    return yaml.safe_dump_all(docs, sort_keys=False)


def nodepool_manifest(env: Env) -> str:
    """EKS Auto Mode の arm64（Graviton）のノードプール。組み込みの general-purpose は amd64 だけなので、
    カスタムイメージ（linux/arm64）をそのまま使うために自前で持つ。NodeClass default は組み込みの system を
    有効にしていると用意される（変更設計書 3.4 節）。"""
    doc = {
        "apiVersion": "karpenter.sh/v1", "kind": "NodePool",
        "metadata": {"name": "lab-arm64"},
        "spec": {
            "template": {"spec": {
                "nodeClassRef": {"group": "eks.amazonaws.com", "kind": "NodeClass", "name": "default"},
                "requirements": [
                    {"key": "kubernetes.io/arch", "operator": "In", "values": ["arm64"]},
                    {"key": "eks.amazonaws.com/instance-category", "operator": "In", "values": ["c", "m", "r", "t"]},
                    {"key": "eks.amazonaws.com/instance-generation", "operator": "Gt", "values": ["5"]},
                    {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand"]},
                ],
            }},
            "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "2m",
                           "budgets": [{"nodes": "1"}]},
            "limits": {"cpu": env.get("LAB_AWS_NODEPOOL_CPU_LIMIT", "32")},
        },
    }
    return yaml.safe_dump(doc, sort_keys=False)


def efs_manifests(out: dict) -> str:
    """chatgpt/ のトークンを全 Pod で共有する EFS の StorageClass と PVC（アクセスポイントで動的に切り出す）。"""
    docs = [
        {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
         "metadata": {"name": "lab-efs"},
         "provisioner": "efs.csi.aws.com",
         "parameters": {"provisioningMode": "efs-ap", "fileSystemId": out["EfsFileSystemId"],
                        "directoryPerms": "700", "uid": "1000", "gid": "1000", "basePath": "/lab"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "chatgpt-token", "namespace": "litellm-lab"},
         "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": "lab-efs",
                  "resources": {"requests": {"storage": "1Gi"}}}},
    ]
    return yaml.safe_dump_all(docs, sort_keys=False)


def eso_store_manifest(env: Env) -> str:
    """External Secrets Operator の ClusterSecretStore。auth を書かず、コントローラーの Pod Identity を使う。"""
    doc = {"apiVersion": "external-secrets.io/v1", "kind": "ClusterSecretStore",
           "metadata": {"name": ESO_STORE},
           "spec": {"provider": {"aws": {"service": "SecretsManager", "region": env.aws_region}}}}
    return yaml.safe_dump(doc, sort_keys=False)


def _external_secret(name: str, data: list[dict] | None = None, extract: str | None = None,
                     template: dict | None = None, secret_type: str | None = None) -> dict:
    spec: dict = {"refreshInterval": "1h",
                  "secretStoreRef": {"kind": "ClusterSecretStore", "name": ESO_STORE},
                  "target": {"name": name, "creationPolicy": "Owner"}}
    if template or secret_type:
        tpl: dict = {"engineVersion": "v2"}
        if secret_type:
            tpl["type"] = secret_type
        if template:
            tpl["data"] = template
        spec["target"]["template"] = tpl
    if extract:
        spec["dataFrom"] = [{"extract": {"key": extract}}]
    if data:
        spec["data"] = data
    return {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
            "metadata": {"name": name, "namespace": "litellm-lab"}, "spec": spec}


def external_secrets_manifest(env: Env, out: dict) -> str:
    """Secrets Manager → Kubernetes の Secret（名前とキーは Mac と同じ。変更仕様書 2.3 節 secrets）。
    - litellm-secrets: アプリの秘密情報（{prefix}/app）＋ Valkey の認証トークン
    - litellm-pg-app / litellm-migrate-db: Aurora のマスターユーザー（RDS が Secrets Manager に置く）
    - audit-viewer-oidc / audit-viewer-session / workspace-mcp: アプリの秘密情報から該当する項目だけ"""
    app = f"{env.aws_prefix}/app"
    aurora = out["AuroraSecretName"]
    redis = out["ValkeyAuthSecretName"]

    def pick(*keys: str) -> list[dict]:
        return [{"secretKey": k, "remoteRef": {"key": app, "property": k}} for k in keys]

    litellm = _external_secret("litellm-secrets", extract=app, data=[
        {"secretKey": "REDIS_PASSWORD", "remoteRef": {"key": redis}},
    ], template={
        # 取り出した項目はそのまま渡し、Google の SSO 用の名前（GOOGLE_CLIENT_*）を OAuth クライアントから作る
        "LITELLM_MASTER_KEY": "{{ .LITELLM_MASTER_KEY }}",
        "LITELLM_SALT_KEY": "{{ .LITELLM_SALT_KEY }}",
        "REDIS_PASSWORD": "{{ .REDIS_PASSWORD }}",
        "GOOGLE_CLIENT_ID": "{{ .GOOGLE_OAUTH_CLIENT_ID | default \"\" }}",
        "GOOGLE_CLIENT_SECRET": "{{ .GOOGLE_OAUTH_CLIENT_SECRET | default \"\" }}",
        "GOOGLE_OAUTH_CLIENT_ID": "{{ .GOOGLE_OAUTH_CLIENT_ID | default \"\" }}",
        "GOOGLE_OAUTH_CLIENT_SECRET": "{{ .GOOGLE_OAUTH_CLIENT_SECRET | default \"\" }}",
    })
    docs = [
        litellm,
        _external_secret("litellm-pg-app", extract=aurora,
                         template={"username": "{{ .username }}", "password": "{{ .password }}"}),
        _external_secret("litellm-migrate-db", extract=aurora, template={
            "DATABASE_URL": "postgresql://{{ .username }}:{{ .password | urlquery }}@{{ .host }}:{{ .port }}/"
                            "{{ .dbname }}?sslmode=require"}),
        _external_secret("audit-viewer-session", data=pick("AUDIT_SESSION_SECRET")),
    ]
    if env.has_google() or env.get("LAB_AUDIT_OIDC_CLIENT_ID"):
        docs.append(_external_secret("audit-viewer-oidc", extract=app, template={
            "AUDIT_OIDC_CLIENT_ID": "{{ .LAB_AUDIT_OIDC_CLIENT_ID | default .GOOGLE_OAUTH_CLIENT_ID }}",
            "AUDIT_OIDC_CLIENT_SECRET": "{{ .LAB_AUDIT_OIDC_CLIENT_SECRET | default .GOOGLE_OAUTH_CLIENT_SECRET }}"}))
    if env.has_google():
        docs.append(_external_secret("workspace-mcp", data=pick("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")))
    return yaml.safe_dump_all(docs, sort_keys=False)


def alb_ingress_manifest(env: Env, out: dict, viewer: bool) -> str:
    """ALB の入口（EKS Auto Mode）。Traefik の k8s/ingress.yaml と同じ経路の決まり:
    パススルーの経路は 403、MCP の経路への GET は 405、/lab-audit/ は検索アプリ、残りは LiteLLM。
    ALB の規則は書いた順に評価される。パスの条件は * と ? のワイルドカードだけ（正規表現は使わない）。"""
    deny = {"type": "fixed-response", "fixedResponseConfig": {"contentType": "text/plain", "statusCode": "403",
                                                              "messageBody": "passthrough is disabled"}}
    not_allowed = {"type": "fixed-response", "fixedResponseConfig": {"contentType": "text/plain", "statusCode": "405",
                                                                     "messageBody": "method not allowed"}}
    annotations = {
        "alb.ingress.kubernetes.io/listen-ports": json.dumps([{"HTTP": 80}, {"HTTPS": 443}]),
        "alb.ingress.kubernetes.io/ssl-redirect": "443",
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/healthcheck-path": "/health/liveliness",
        # Pod を止めるとき、ALB が送り先から外すのを待つ時間（LiteLLM の preStop の 10 秒と合わせる）
        "alb.ingress.kubernetes.io/target-group-attributes": "deregistration_delay.timeout_seconds=30",
        "alb.ingress.kubernetes.io/actions.deny-passthrough": json.dumps(deny),
        "alb.ingress.kubernetes.io/actions.mcp-get-not-allowed": json.dumps(not_allowed),
        "alb.ingress.kubernetes.io/conditions.mcp-get-not-allowed": json.dumps(
            [{"field": "http-request-method", "httpRequestMethodConfig": {"Values": ["GET"]}}]),
    }

    def path(p: str, backend: dict) -> dict:
        return {"path": p, "pathType": "ImplementationSpecific", "backend": backend}

    action = lambda name: {"service": {"name": name, "port": {"name": "use-annotation"}}}  # noqa: E731
    svc = lambda name, port: {"service": {"name": name, "port": {"number": port}}}  # noqa: E731
    paths = []
    for provider in PASSTHROUGH_PROVIDERS:
        paths += [path(f"/{provider}", action("deny-passthrough")), path(f"/{provider}/*", action("deny-passthrough"))]
    paths.append(path("/*/mcp", action("mcp-get-not-allowed")))
    if viewer:
        paths += [path("/lab-audit", svc("audit-viewer", 8000)), path("/lab-audit/*", svc("audit-viewer", 8000))]
    paths.append(path("/*", svc("litellm", 4000)))
    docs = [
        {"apiVersion": "eks.amazonaws.com/v1", "kind": "IngressClassParams", "metadata": {"name": "lab-alb"},
         "spec": {"scheme": env.get("LAB_AWS_ALB_SCHEME", "internet-facing"),
                  "certificateARNs": [out["CertificateArn"]],
                  "group": {"name": env.aws_prefix},
                  "loadBalancerAttributes": [{"key": "idle_timeout.timeout_seconds", "value": "600"}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "IngressClass", "metadata": {"name": "lab-alb"},
         "spec": {"controller": "eks.amazonaws.com/alb",
                  "parameters": {"apiGroup": "eks.amazonaws.com", "kind": "IngressClassParams", "name": "lab-alb"}}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
         "metadata": {"name": "litellm", "namespace": "litellm-lab", "annotations": annotations},
         "spec": {"ingressClassName": "lab-alb", "rules": [{"http": {"paths": paths}}]}},
    ]
    return yaml.safe_dump_all(docs, sort_keys=False)


# ---- VM 3 台の環境（LAB_TARGET=aws-ec2 / gcp-vm）。変更設計書 .history/2026-10-25-EC2三台構成/ 2 章、4.6 節 ----
VM_REGISTRY_NAME = "lab-registry"
VM_REGISTRY_IMAGE = "registry:3.0.0"
VM_TLS_SECRET = "litellm-tls"
ACME_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
ACME_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def vm_registry_manifest(env: Env, bucket: str, node_port: int) -> str:
    """クラスタの中のイメージ置き場（Docker の registry。保存先は S3 の images バケットなので、どの台が落ちても続く）。
    2 Pod を別々の台に置き、NodePort で各ノードが自分の IP から引く（containerd のミラー設定は vm/node-init.sh）。
    Mac からは ./lab images が kubectl port-forward で push する。"""
    labels = {"app": VM_REGISTRY_NAME}
    env_vars = [
        {"name": "REGISTRY_STORAGE", "value": "s3"},
        {"name": "REGISTRY_STORAGE_S3_REGION", "value": env.vm_region},
        {"name": "REGISTRY_STORAGE_S3_REGIONENDPOINT", "value": vm_s3_endpoint(env)},
        {"name": "REGISTRY_STORAGE_S3_BUCKET", "value": bucket},
        {"name": "REGISTRY_STORAGE_S3_ROOTDIRECTORY", "value": "/registry"},
        {"name": "REGISTRY_STORAGE_DELETE_ENABLED", "value": "true"},
        {"name": "REGISTRY_HTTP_ADDR", "value": ":5000"},
    ]
    secret_ref = [{"secretRef": {"name": VM_REGISTRY_NAME}}]
    docs = [
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": VM_REGISTRY_NAME, "namespace": "litellm-lab"},
         "spec": {"type": "NodePort", "selector": labels,
                  "ports": [{"port": 5000, "targetPort": 5000, "nodePort": node_port}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": VM_REGISTRY_NAME, "namespace": "litellm-lab"},
         "spec": {"replicas": 2, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                          {"weight": 100, "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname",
                                                              "labelSelector": {"matchLabels": labels}}}]}},
                      "tolerations": [
                          {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute",
                           "tolerationSeconds": 30},
                          {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute",
                           "tolerationSeconds": 30}],
                      "containers": [{"name": "registry", "image": VM_REGISTRY_IMAGE, "env": env_vars,
                                      "envFrom": secret_ref,
                                      "ports": [{"containerPort": 5000}],
                                      "readinessProbe": {"httpGet": {"path": "/v2/", "port": 5000}, "periodSeconds": 5},
                                      "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                                    "limits": {"memory": "256Mi"}}}]}}}},
    ]
    return yaml.safe_dump_all(docs, sort_keys=False)


def vm_registry_secret(env: Env) -> dict[str, str]:
    """registry の Pod に渡す秘密: S3 の静的キー（IAM ユーザー registry）と、2 Pod で共通の HTTP の秘密。"""
    key_name, secret_name = s3_env_names("registry")
    return {"REGISTRY_STORAGE_S3_ACCESSKEY": env.get(key_name), "REGISTRY_STORAGE_S3_SECRETKEY": env.get(secret_name),
            "REGISTRY_HTTP_SECRET": env.get("LAB_REGISTRY_HTTP_SECRET")}


def acme_staging(env: Env) -> bool:
    return env.get("LAB_ACME_STAGING", "false").lower() == "true"


def vm_issuer_manifest(env: Env) -> str:
    """Let's Encrypt の ClusterIssuer（DNS-01、Cloudflare）。API トークンは cert-manager の名前空間の Secret。"""
    email = env.get("LAB_ACME_EMAIL")
    if not email:
        raise ValueError(f"LAB_TARGET={env.target} には LAB_ACME_EMAIL（Let's Encrypt の連絡先）が要ります")
    doc = {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer",
           "metadata": {"name": "letsencrypt"},
           "spec": {"acme": {
               "server": ACME_STAGING if acme_staging(env) else ACME_PRODUCTION,
               "email": email,
               "privateKeySecretRef": {"name": "letsencrypt-account"},
               "solvers": [{"dns01": {"cloudflare": {
                   "apiTokenSecretRef": {"name": "cloudflare-api-token", "key": "api-token"}}}}]}}}
    return yaml.safe_dump(doc, sort_keys=False)


def vm_certificate_manifest(env: Env) -> str:
    doc = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
           "metadata": {"name": VM_TLS_SECRET, "namespace": "litellm-lab"},
           "spec": {"secretName": VM_TLS_SECRET, "dnsNames": [env.hostname],
                    "issuerRef": {"name": "letsencrypt", "kind": "ClusterIssuer"},
                    "privateKey": {"rotationPolicy": "Always"}}}
    return yaml.safe_dump(doc, sort_keys=False)


def tls_ingressroutes(manifest_text: str, secret_name: str = VM_TLS_SECRET) -> str:
    """マニフェストの中の IngressRoute（web）を、websecure に TLS 付きで写す（名前は <元の名前>-tls）。
    経路と Middleware は共通の定義（k8s/ingress.yaml、audit-viewer.yaml）をそのまま使い、入口の HTTPS 化だけを足す。"""
    docs = []
    for d in yaml.safe_load_all(manifest_text):
        if not d or d.get("kind") != "IngressRoute":
            continue
        copy = json.loads(json.dumps(d))
        copy["metadata"]["name"] = d["metadata"]["name"] + "-tls"
        copy["spec"]["entryPoints"] = ["websecure"]
        copy["spec"]["tls"] = {"secretName": secret_name}
        docs.append(copy)
    return yaml.safe_dump_all(docs, sort_keys=False)


def pg_backup_values(env: Env) -> dict[str, str]:
    """k8s/postgres-cnpg.yaml に渡す値。Mac は RustFS（endpointURL あり、接頭辞なし）、VM の環境はクラウドのバケット
    （接頭辞付き、リージョンはクラウド。endpointURL は AWS は省き、GCP は storage.googleapis.com）。"""
    if env.is_vm:
        endpoint = vm_s3_endpoint(env)
        return {"S3_BUCKET_PREFIX": bucket_name(env, ""), "S3_ENDPOINT_LINE": f"endpointURL: {endpoint}" if endpoint else "",
                "S3_REGION": env.vm_region}
    return {"S3_BUCKET_PREFIX": "", "S3_ENDPOINT_LINE": f"endpointURL: {env.get('LAB_S3_ENDPOINT', DEFAULT_S3_ENDPOINT)}",
            "S3_REGION": env.get("LAB_S3_REGION", "us-east-1")}
