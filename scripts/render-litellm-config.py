#!/usr/bin/env python3
"""LiteLLM のプロキシ設定（config.yaml）を .env の内容から生成する（標準ライブラリだけで動く）。

手作業の準備が済んでいない部分は、設定に含めない（docs/design.md 6.3）。
- Google の OAuth クライアントが無い → Gmail の MCP を含めない
- TypeSafe の API キーが無い → 一次判定（Jev）を無効にする
- ChatGPT のトークンが無い → Codex 用のモデルを含めない（起動時にログインを始めて止まるため）。
  二次判定は試験用の偽 LLM（fake-llm）を使う
"""

import json
import os
import sys


def env(name, default=""):
    return os.environ.get(name, default).strip()


def q(value):
    """YAML の文字列として安全に書く（JSON の文字列は YAML として有効）。"""
    return json.dumps(value, ensure_ascii=False)


has_google = bool(env("GOOGLE_OAUTH_CLIENT_ID") and env("GOOGLE_OAUTH_CLIENT_SECRET"))
has_jev = bool(env("TYPESAFE_API_KEY"))
has_chatgpt = env("LAB_HAS_CHATGPT") == "true"
use_jev = has_jev and env("LAB_USE_JEV", "true").lower() == "true"
codex_model = env("LAB_CODEX_MODEL", "gpt-5.3-codex")
judge_model = env("LAB_JUDGE_MODEL", "gpt-5.1-codex-mini")
fake_base = "http://fake-llm.litellm-lab.svc.cluster.local:8000/v1"

out = []
w = out.append

w("# scripts/render-litellm-config.py が生成。手で編集しない")
w("model_list:")
w("  # Claude Code（Claude Max のトークンを転送する。api_key は書かない）")
w('  - model_name: "anthropic/*"')
w("    litellm_params:")
w('      model: "anthropic/*"')
if has_chatgpt:
    w("  # Codex（ChatGPT Plus）")
    w(f"  - model_name: {q(codex_model)}")
    w("    litellm_params:")
    w(f"      model: {q('chatgpt/' + codex_model)}")
else:
    # chatgpt/ のモデルをトークン無しで設定すると、LiteLLM が起動の途中でデバイスコードの
    # ログインを始めて止まる（2026-10-11 に確認）。トークンを取るまで含めない
    w("  # Codex（ChatGPT Plus）: トークンが無いので未設定。make chatgpt-login の後に make up で入る")
w("  # 審査の試験用の偽 LLM（tests/fake-llm）。ツールコールを返す")
w("  - model_name: fake-tools")
w("    litellm_params:")
w("      model: openai/fake-tools")
w("      api_key: sk-fake")
w(f"      api_base: {q(fake_base)}")
w("  # 二次判定")
w("  - model_name: review-judge")
w("    litellm_params:")
if has_chatgpt:
    w(f"      model: {q('chatgpt/' + judge_model)}")
else:
    w("      # ChatGPT のトークンが無いので、試験用の偽 LLM で代用している")
    w("      model: openai/fake-tools")
    w("      api_key: sk-fake")
    w(f"      api_base: {q(fake_base)}")
w("")
w("guardrails:")
w("  - guardrail_name: tool-call-review")
w("    litellm_params:")
w("      guardrail: tool_call_review.ToolCallReview")
w("      mode: [post_call]")
w("      default_on: true")
w(f"      use_jev: {'true' if use_jev else 'false'}")
w("      jev_model: typesafe/jev-latest")
w(f"      jev_threshold: {float(env('LAB_JEV_THRESHOLD', '0.2'))}")
w("      judge_model: review-judge")
w("      fail_open: false")

if has_google:
    client_id = env("GOOGLE_OAUTH_CLIENT_ID")
    w("")
    w("mcp_servers:")
    w("  gmail:")
    w('    url: "http://workspace-mcp.litellm-lab.svc.cluster.local:8000/mcp"')
    w('    transport: "http"')
    w('    auth_type: "oauth2"')
    w('    oauth2_flow: "authorization_code"')
    w("    client_id: os.environ/GOOGLE_OAUTH_CLIENT_ID")
    w("    client_secret: os.environ/GOOGLE_OAUTH_CLIENT_SECRET")
    w('    authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent"')
    w('    token_url: "https://oauth2.googleapis.com/token"')
    w("    scopes:")
    for s in ("openid",
              "https://www.googleapis.com/auth/userinfo.email",
              "https://www.googleapis.com/auth/userinfo.profile",
              "https://www.googleapis.com/auth/gmail.readonly"):
        w(f"      - {q(s)}")
    w("    # Gmail にサインインしたアカウントと LiteLLM のユーザーのメールが違えば拒否する")
    w("    oauth_identity_binding:")
    w("      mode: enforce")
    w('      issuer: "https://accounts.google.com"')
    w(f"      audiences: [{q(client_id)}]")
    w("      principal_claim: email")
    w("      caller_field: user_email")
    w("      require_email_verified: true")

w("")
w("router_settings:")
w("  redis_host: os.environ/REDIS_HOST")
w("  redis_port: os.environ/REDIS_PORT")
w("  redis_password: os.environ/REDIS_PASSWORD")
w("  routing_strategy: simple-shuffle")
w("  disable_cooldowns: true   # Claude Max のトークン切れの 1 人のせいで、他の人まで止まらないように")
w("")
w("general_settings:")
w("  master_key: os.environ/LITELLM_MASTER_KEY")
w("  database_url: os.environ/DATABASE_URL")
w("  database_connection_pool_limit: 5   # PostgreSQL の上限 100 ÷ 最大 4 Pod に余裕を持たせる")
w("  proxy_batch_write_at: 60")
w("  require_key_mcp_access_defined: true")

sys.stdout.write("\n".join(out) + "\n")
