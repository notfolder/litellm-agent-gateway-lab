"""構築ツールの単体テスト（クラスタや Docker を使わない部分）。./lab test-unit で流す。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from labtool import checks, render
from labtool.core import Env, has_chatgpt_token, parse_env, render_template, secret_manifest, set_env_line

BASE = {"LITELLM_MASTER_KEY": "sk-master", "LAB_HTTP_PORT": "8080"}


def env(**extra):
    return Env({**BASE, **extra})


class EnvFileTest(unittest.TestCase):
    def test_parse_skips_comments_and_strips_quotes(self):
        text = "# c\nA=1\n  B = 'two'\nC=\n#D=4\nE=\"x=y\"\n"
        self.assertEqual(parse_env(text), {"A": "1", "B": "two", "C": "", "E": "x=y"})

    def test_set_replaces_only_target_and_keeps_comments(self):
        text = "# head\nA=\n#A=commented\nB=2\n"
        self.assertEqual(set_env_line(text, "A", "v"), "# head\nA=v\n#A=commented\nB=2\n")

    def test_set_appends_missing_key(self):
        self.assertEqual(set_env_line("A=1\n", "B", "2"), "A=1\nB=2\n")


class ChatgptTokenTest(unittest.TestCase):
    def check(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "auth.json"
            if content is not None:
                p.write_text(content)
            return has_chatgpt_token(p)

    def test_token_present(self):
        self.assertTrue(self.check(json.dumps({"refresh_token": "r"})))
        self.assertTrue(self.check(json.dumps({"access_token": "a"})))

    def test_leftover_without_token_is_not_a_login(self):
        # ログインが途中で止まると、要求時刻だけのファイルが残る
        self.assertFalse(self.check(json.dumps({"requested_at": 1})))
        self.assertFalse(self.check("not json"))
        self.assertFalse(self.check(None))


class LitellmConfigTest(unittest.TestCase):
    def models(self, cfg):
        return {m["model_name"]: m["litellm_params"]["model"] for m in cfg["model_list"]}

    def test_without_manual_setup(self):
        cfg = render.litellm_config(env(), has_chatgpt=False)
        models = self.models(cfg)
        self.assertNotIn("gpt-5.6-terra", models)  # トークン無しで chatgpt/ を入れると起動が止まる
        self.assertEqual(models["review-judge"], "openai/fake-tools")
        self.assertNotIn("mcp_servers", cfg)
        self.assertFalse(cfg["guardrails"][0]["litellm_params"]["use_jev"])

    def test_with_everything(self):
        e = env(GOOGLE_OAUTH_CLIENT_ID="cid", GOOGLE_OAUTH_CLIENT_SECRET="GOCSPX-unit", TYPESAFE_API_KEY="ts-unit",
                LAB_CODEX_MODEL="m1", LAB_JUDGE_MODEL="m2", LAB_JEV_THRESHOLD="0.3")
        cfg = render.litellm_config(e, has_chatgpt=True)
        models = self.models(cfg)
        self.assertEqual(models["m1"], "chatgpt/m1")
        self.assertEqual(models["review-judge"], "chatgpt/m2")
        params = cfg["guardrails"][0]["litellm_params"]
        self.assertTrue(params["use_jev"])
        self.assertEqual(params["jev_threshold"], 0.3)
        gmail = cfg["mcp_servers"]["gmail"]
        self.assertEqual(gmail["oauth_identity_binding"]["audiences"], ["cid"])
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", gmail["scopes"])
        # 秘密情報は設定に書かず、環境変数で参照する
        text = render.to_yaml(cfg)
        self.assertNotIn("GOCSPX-unit", text)
        self.assertNotIn("ts-unit", text)

    def test_jev_can_be_turned_off(self):
        cfg = render.litellm_config(env(TYPESAFE_API_KEY="t", LAB_USE_JEV="false"), has_chatgpt=False)
        self.assertFalse(cfg["guardrails"][0]["litellm_params"]["use_jev"])

    def test_yaml_round_trip_is_stable(self):
        cfg = render.litellm_config(env(), has_chatgpt=False)
        text = render.to_yaml(cfg, "header")
        self.assertTrue(text.startswith("# header\n"))
        self.assertEqual(yaml.safe_load(text), cfg)
        self.assertEqual(text, render.to_yaml(render.litellm_config(env(), has_chatgpt=False), "header"))


class SecretsTest(unittest.TestCase):
    def test_secret_manifest_is_base64_and_sorted(self):
        m = json.loads(secret_manifest("s", {"b": "2", "a": "1"}))
        self.assertEqual(list(m["data"]), ["a", "b"])
        self.assertEqual(base64.b64decode(m["data"]["b"]).decode(), "2")

    def test_litellm_secrets_follow_manual_setup(self):
        self.assertNotIn("GOOGLE_CLIENT_ID", render.secrets_for_litellm(env()))
        data = render.secrets_for_litellm(env(GOOGLE_OAUTH_CLIENT_ID="c", GOOGLE_OAUTH_CLIENT_SECRET="s",
                                              TYPESAFE_API_KEY="t"))
        self.assertEqual(data["GOOGLE_CLIENT_ID"], "c")
        self.assertEqual(data["GOOGLE_OAUTH_CLIENT_SECRET"], "s")
        self.assertEqual(data["TYPESAFE_API_KEY"], "t")


class ClientFilesTest(unittest.TestCase):
    def test_master_key_when_no_users(self):
        self.assertEqual(render.client_keys(env(), "1"), ("sk-master", "sk-master"))

    def test_user_keys(self):
        e = env(LAB_USER1_EMAIL="a@x", LAB_KEY_USER2_CLAUDE="c2", LAB_KEY_USER2_CODEX="x2")
        self.assertEqual(render.client_keys(e, "2"), ("c2", "x2"))

    def test_gmail_only_with_google(self):
        files = render.client_files(env(), "kc", "kx", Path("/tmp/codex"))
        self.assertEqual(json.loads(files["mcp.json"]), {"mcpServers": {}})
        self.assertNotIn("mcp_servers.gmail", files["codex-config.toml"])
        files = render.client_files(env(GOOGLE_OAUTH_CLIENT_ID="c", GOOGLE_OAUTH_CLIENT_SECRET="s"),
                                    "kc", "kx", Path("/tmp/codex"))
        self.assertEqual(json.loads(files["mcp.json"])["mcpServers"]["gmail"]["url"],
                         "http://localhost:8080/gmail/mcp")
        self.assertIn("[mcp_servers.gmail]", files["codex-config.toml"])
        self.assertIn("LITELLM_API_KEY='kx'", files["codex.env"])
        # MCP のツールの一覧を最初のターンの前に待つ（待たないと gmail のツールが使えない回がある）
        self.assertIn("mcp_optional_startup_grace_ms = 10000", files["codex-config.toml"])


class TemplateTest(unittest.TestCase):
    def test_missing_value_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.yaml"
            p.write_text("a: ${A}\nb: ${B}\n")
            self.assertEqual(render_template(p, {"A": 1, "B": "x"}), "a: 1\nb: x\n")
            with self.assertRaises(KeyError):
                render_template(p, {"A": 1})


class ImageTagTest(unittest.TestCase):
    def test_tag_changes_with_content_but_not_with_pycache(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d)
            (ctx / "guardrails" / "__pycache__").mkdir(parents=True)
            (ctx / "Dockerfile").write_text("FROM x\n")
            (ctx / "guardrails" / "g.py").write_text("a = 1\n")
            t1 = render.image_tag("base:1", ctx)
            (ctx / "guardrails" / "__pycache__" / "g.pyc").write_bytes(b"\0")
            self.assertEqual(render.image_tag("base:1", ctx), t1)
            self.assertNotEqual(render.image_tag("base:2", ctx), t1)
            (ctx / "guardrails" / "g.py").write_text("a = 2\n")
            self.assertNotEqual(render.image_tag("base:1", ctx), t1)


class ReviewLogTest(unittest.TestCase):
    def test_summary(self):
        records = [
            {"event": "pass_through", "client": "claude-code"},
            {"event": "review", "client": "codex", "allowed": True, "judge_used": False,
             "timings_ms": {"total": 10}},
            {"event": "review", "client": "codex", "allowed": False, "judge_used": True, "error": None,
             "timings_ms": {"total": 30}, "ts": "t1", "tools": ["shell"], "reason": "削除"},
        ]
        text = checks.summarize_reviews(records)
        self.assertIn("判定 2 件（素通し 1 件）", text)
        self.assertIn("許可 1 件 / 拒否 1 件 / 判定エラー 0 件", text)
        self.assertIn("二次判定まで進んだ割合: 50.0%", text)
        self.assertIn("平均 20 / 最大 30", text)
        self.assertIn("t1 codex shell: 削除", text)


if __name__ == "__main__":
    unittest.main()
