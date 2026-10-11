""".env の内容から設定を作る、副作用のない関数（単体テストの対象）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from .core import CHATGPT_TOKEN_IN_NODE, Env

FAKE_LLM_BASE = "http://fake-llm.litellm-lab.svc.cluster.local:8000/v1"
WORKSPACE_MCP_URL = "http://workspace-mcp.litellm-lab.svc.cluster.local:8000/mcp"
GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
]


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
    use_jev = env.has_jev() and env.get("LAB_USE_JEV", "true").lower() == "true"

    # Claude Code（Claude Max のトークンを転送する。api_key は書かない）
    model_list: list[dict] = [{"model_name": "anthropic/*", "litellm_params": {"model": "anthropic/*"}}]
    if has_chatgpt:
        # Codex（ChatGPT Plus）
        model_list.append({"model_name": codex_model, "litellm_params": {"model": f"chatgpt/{codex_model}"}})
    # 審査の試験用の偽 LLM（tests/fake-llm）。ツールコールを返す
    model_list.append({"model_name": "fake-tools", "litellm_params": _fake_params()})
    # 二次判定
    model_list.append({
        "model_name": "review-judge",
        "litellm_params": {"model": f"chatgpt/{judge_model}"} if has_chatgpt else _fake_params(),
    })

    config: dict = {
        "model_list": model_list,
        "guardrails": [{
            "guardrail_name": "tool-call-review",
            "litellm_params": {
                "guardrail": "tool_call_review.ToolCallReview",
                "mode": ["post_call"],
                "default_on": True,
                "use_jev": use_jev,
                "jev_model": "typesafe/jev-latest",
                "jev_threshold": float(env.get("LAB_JEV_THRESHOLD", "0.2")),
                "judge_model": "review-judge",
                "fail_open": False,
                # 疎通確認（偽 LLM の fake-tools）は、結果が決まるように偽 LLM の審査官で判定する
                "test_model": "fake-tools",
                "test_judge_model": "fake-tools",
            },
        }],
    }

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

    config["router_settings"] = {
        "redis_host": "os.environ/REDIS_HOST",
        "redis_port": "os.environ/REDIS_PORT",
        "redis_password": "os.environ/REDIS_PASSWORD",
        "routing_strategy": "simple-shuffle",
        # Claude Max のトークン切れの 1 人のせいで、他の人まで止まらないように
        "disable_cooldowns": True,
    }
    config["general_settings"] = {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "database_url": "os.environ/DATABASE_URL",
        # PostgreSQL の上限 100 ÷ 最大 4 Pod に余裕を持たせる
        "database_connection_pool_limit": 5,
        "proxy_batch_write_at": 60,
        "require_key_mcp_access_defined": True,
    }
    return config


def to_yaml(data: dict, header: str = "") -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return (f"# {header}\n" if header else "") + body


def helm_overrides(env: Env, image_repo: str, image_tag: str, config_hash: str) -> dict:
    """values.yaml に重ねる、環境ごとの値。"""
    return {
        "image": {"repository": image_repo, "tag": image_tag},
        # 設定のハッシュを Pod の注釈に入れ、設定が変わったときだけ Pod を作り直す
        "podAnnotations": {"lab/config-hash": config_hash},
        "envVars": {
            "LITELLM_MODE": "PRODUCTION",
            "PROXY_BASE_URL": env.base_url,
            "CHATGPT_TOKEN_DIR": CHATGPT_TOKEN_IN_NODE,
            "UI_USERNAME": "admin",
        },
    }


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
        "REDIS_HOST": "redis",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": env.get("REDIS_PASSWORD"),
    }
    if env.has_google():
        # 管理画面の Google SSO（GOOGLE_*）と Gmail の MCP（GOOGLE_OAUTH_*）で同じクライアントを使う
        cid, secret = env.get("GOOGLE_OAUTH_CLIENT_ID"), env.get("GOOGLE_OAUTH_CLIENT_SECRET")
        data |= {"GOOGLE_CLIENT_ID": cid, "GOOGLE_CLIENT_SECRET": secret,
                 "GOOGLE_OAUTH_CLIENT_ID": cid, "GOOGLE_OAUTH_CLIENT_SECRET": secret}
    if env.has_jev():
        data["TYPESAFE_API_KEY"] = env.get("TYPESAFE_API_KEY")
    return data


def client_keys(env: Env, user: str) -> tuple[str, str]:
    """クライアントが使うキー（Claude Code 用、Codex 用）。ユーザーが未設定ならマスターキー。"""
    if not env.has_users():
        master = env.get("LITELLM_MASTER_KEY")
        return master, master
    return env.get(f"LAB_KEY_USER{user}_CLAUDE"), env.get(f"LAB_KEY_USER{user}_CODEX")


def client_files(env: Env, claude_key: str, codex_key: str, codex_home: Path) -> dict[str, str]:
    """Claude Code と Codex の検証用の設定。キーは clients/out/ からの相対パス、値は中身。"""
    base = env.base_url
    files: dict[str, str] = {}
    files["claude.env"] = (
        "# clients/claude-lab が読み込む（生成物。手で編集しない）\n"
        f"export ANTHROPIC_BASE_URL='{base}'\n"
        f"export ANTHROPIC_CUSTOM_HEADERS='x-litellm-api-key: Bearer {claude_key}'\n"
        f"export LAB_LITELLM_KEY='{claude_key}'\n"
    )
    host = f"localhost:{env.http_port}"
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
    return files
