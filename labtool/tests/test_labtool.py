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

    def test_isolated_review(self):
        cfg = render.litellm_config(env(LAB_JUDGE_MODEL="m2"), has_chatgpt=True)
        self.assertEqual(self.models(cfg)["isolated-judge"], "chatgpt/m2")   # 空なら LAB_JUDGE_MODEL と同じ
        params = cfg["guardrails"][0]["litellm_params"]
        self.assertTrue(params["isolated_review"])
        self.assertTrue(params["isolated_review_claude_code"])
        self.assertEqual(params["isolated_judge_model"], "isolated-judge")
        self.assertEqual(params["test_isolated_judge_model"], "fake-tools")
        self.assertFalse(params["isolated_fail_open"])
        self.assertIn("社外に出る", params["isolated_policy"])               # 既定の判定基準のファイルの中身
        cfg = render.litellm_config(env(LAB_ISOLATED_JUDGE_MODEL="m3", LAB_ISOLATED_CLAUDE_CODE="false"),
                                    has_chatgpt=False)
        self.assertEqual(self.models(cfg)["isolated-judge"], "openai/fake-tools")
        self.assertFalse(cfg["guardrails"][0]["litellm_params"]["isolated_review_claude_code"])
        self.assertEqual(self.models(render.litellm_config(env(LAB_ISOLATED_JUDGE_MODEL="m3"), has_chatgpt=True))
                         ["isolated-judge"], "chatgpt/m3")

    def test_isolated_content(self):
        params = render.litellm_config(env(), has_chatgpt=True)["guardrails"][0]["litellm_params"]
        self.assertEqual((params["isolated_content"], params["isolated_content_max_bytes"],
                          params["isolated_content_max_files"]), (True, 24576, 5))   # 既定は入り
        self.assertEqual(params["isolated_destinations"], ["private"])
        self.assertEqual(params["mode"], ["pre_call", "post_call"])              # 方針の差し込みに pre_call が要る
        cfg = render.litellm_config(env(LAB_ISOLATED_CONTENT="false", LAB_ISOLATED_CONTENT_MAX_BYTES="100",
                                        LAB_ISOLATED_CONTENT_MAX_FILES="1",
                                        LAB_ISOLATED_DESTINATIONS="private, corp.example,203.0.113.0/24"),
                                    has_chatgpt=True)
        params = cfg["guardrails"][0]["litellm_params"]
        self.assertEqual((params["isolated_content"], params["isolated_content_max_bytes"],
                          params["isolated_content_max_files"]), (False, 100, 1))
        self.assertEqual(params["isolated_destinations"], ["private", "corp.example", "203.0.113.0/24"])
        self.assertEqual(params["mode"], ["post_call"])

    def test_isolated_review_off_still_says_false(self):
        cfg = render.litellm_config(env(LAB_ISOLATED_REVIEW="false"), has_chatgpt=True)
        self.assertNotIn("isolated-judge", self.models(cfg))
        params = cfg["guardrails"][0]["litellm_params"]
        # 出さないとガードレールの既定（true）になり、判定基準が空で起動しなくなる
        self.assertIs(params["isolated_review"], False)
        self.assertNotIn("isolated_policy", params)

    def test_isolated_policy_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.md"
            f.write_text("  独自の基準  \n")
            self.assertEqual(render.isolated_policy(env(LAB_ISOLATED_POLICY_FILE=str(f))), "独自の基準")
            f.write_text("\n")
            with self.assertRaises(ValueError):
                render.isolated_policy(env(LAB_ISOLATED_POLICY_FILE=str(f)))

    def test_isolated_judge_prices_and_contract(self):
        cfg = render.litellm_config(env(LAB_PRICE_ISOLATED_JUDGE_IN="1", LAB_PRICE_ISOLATED_JUDGE_OUT="2",
                                        LAB_PRICE_ISOLATED_JUDGE_CACHE_READ="0.1", LAB_JUDGE_MODEL="unknown"),
                                    has_chatgpt=True)
        iso = next(m for m in cfg["model_list"] if m["model_name"] == "isolated-judge")
        self.assertAlmostEqual(iso["model_info"]["output_cost_per_token"], 2.0e-6)
        plus = next(c for c in render.initial_contracts(env())["contracts"] if c["id"] == "chatgpt-plus")
        self.assertIn("isolated-judge", plus["models"])

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
        files = render.client_files(env(), "kc", "kx", Path("/tmp/codex"), Path("/tmp/codex-nr"))
        self.assertEqual(json.loads(files["mcp.json"]), {"mcpServers": {}})
        self.assertNotIn("mcp_servers.gmail", files["codex-config.toml"])
        files = render.client_files(env(GOOGLE_OAUTH_CLIENT_ID="c", GOOGLE_OAUTH_CLIENT_SECRET="s"),
                                    "kc", "kx", Path("/tmp/codex"), Path("/tmp/codex-nr"))
        self.assertEqual(json.loads(files["mcp.json"])["mcpServers"]["gmail"]["url"],
                         "http://localhost:8080/gmail/mcp")
        self.assertIn("[mcp_servers.gmail]", files["codex-config.toml"])
        self.assertIn("LITELLM_API_KEY='kx'", files["codex.env"])
        # MCP のツールの一覧を最初のターンの前に待つ（待たないと gmail のツールが使えない回がある）
        self.assertIn("mcp_optional_startup_grace_ms = 10000", files["codex-config.toml"])

    def test_noreview_codex_uses_its_own_codex_home(self):
        # Codex は CODEX_HOME ごとに常駐プロセスを立てて起動時のキーを持ち続けるので、審査なしは別にする
        files = render.client_files(env(LAB_NOREVIEW_KEYS="true", LAB_KEY_USER1_CODEX_NOREVIEW="nx"),
                                    "kc", "kx", Path("/tmp/codex"), Path("/tmp/codex-nr"))
        self.assertIn("CODEX_HOME='/tmp/codex-nr'", files["noreview-codex.env"])
        self.assertIn("LITELLM_API_KEY='nx'", files["noreview-codex.env"])
        self.assertEqual(files["noreview-codex-config.toml"], files["codex-config.toml"])
        self.assertNotIn("noreview-codex.env", render.client_files(env(), "kc", "kx", Path("/a"), Path("/b")))


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

    def test_summary_with_isolated_review(self):
        records = [
            {"event": "review", "client": "codex", "allowed": False, "judge_used": True, "context_review": True,
             "isolated_review": True, "isolated_allowed": False, "blocked_by": ["isolated"],
             "timings_ms": {"total": 30, "isolated": 25}},
            {"event": "review", "client": "claude-code", "allowed": True, "judge_used": False,
             "context_review": False, "isolated_review": True, "isolated_allowed": True, "blocked_by": [],
             "timings_ms": {"total": 15, "isolated": 15}},
        ]
        text = checks.summarize_reviews(records)
        self.assertIn("二次判定まで進んだ割合: 100.0%", text)   # Claude Code（文脈あり審査なし）は数えない
        self.assertIn("2 件 / 拒否 1 件 / 判定エラー 0 件", text)
        self.assertIn("中央値 25 / 最大 25", text)
        self.assertIn("isolated 1 件", text)
        self.assertIn("なし 1 件", text)


class JudgeEvalTest(unittest.TestCase):
    def test_scopes(self):
        self.assertEqual(checks.judge_scopes("both"), ["context", "isolated"])
        self.assertEqual(checks.judge_scopes("context"), ["context"])
        self.assertEqual(checks.judge_scopes("isolated"), ["isolated"])

    def test_isolated_prompt_and_input_match_the_guardrail(self):
        system = checks._isolated_system_prompt(env())
        self.assertTrue(system.startswith("[[tool-call-isolated-review]]"))
        self.assertNotIn("{policy}", system)
        self.assertIn("社外に出る", system)
        self.assertIn('{"decision": "allow" または "block"', system)
        cases = json.loads((Path(checks.ROOT) / "tests" / "review-scenarios" / "isolated-cases.json").read_text())
        self.assertGreaterEqual(len(cases), 10)
        text = checks._render_for_isolated(cases[0])
        self.assertTrue(text.startswith("# 審査対象（これから実行しようとしているツールコール）"))
        self.assertIn("## exec_command\n説明: Runs a command", text)
        # 送信するファイルの中身の審査（既定で入り）: 決まりと出力の形が入り、差し込み口が残らない
        self.assertIn("need_content", system)
        self.assertNotIn("{content_rules}", system)
        self.assertNotIn("{output_format}", system)
        self.assertNotIn("{max_files}", system)
        off = checks._isolated_system_prompt(env(LAB_ISOLATED_CONTENT="false"))
        self.assertNotIn("need_content", off)
        self.assertIn("設定: プライベートアドレス", system)
        self.assertIn("corp.example（とその下位のドメイン）",
                      checks._isolated_system_prompt(env(LAB_ISOLATED_DESTINATIONS="private,corp.example")))
        with_content = next(c for c in cases if c.get("contents"))
        self.assertIn("# ゲートウェイが確かめた読み取り", checks._render_for_isolated(with_content))
        with_env = next(c for c in cases if any(x["path"].startswith("env:") for x in c.get("contents") or []))
        self.assertIn("## env:CRM_IMPORT_URL", checks._render_for_isolated(with_env))

    def test_judge_case_mark(self):
        case = {"expect_destination": "10.20.3.20", "expect_runs": ["a.py"], "expect_env": ["URL"]}
        self.assertEqual(checks.judge_case_mark(case, {"decision": "allow", "destination": "http://10.20.3.20:8080/x",
                                                       "runs": ["a.py", "b"], "env": ["URL"]}, ["allow"]), "allow")
        mark = checks.judge_case_mark(case, {"decision": "block", "destination": "", "runs": [], "env": ["URL"]}, ["allow"])
        self.assertTrue(mark.startswith("block×(") and "decision" in mark and "destination=空" in mark and "runs=[]" in mark)
        self.assertEqual(checks.judge_case_mark({"expect_destination": ""}, {"decision": "allow"}, ["allow"]), "allow")
        self.assertEqual(checks.judge_case_mark({"expect_destination": "unknown"}, {"decision": "block", "destination": "unknown"},
                                                ["block"]), "block")


class ObjectStoreSpecTest(unittest.TestCase):
    def test_env_names(self):
        self.assertEqual(render.s3_env_names("litellm-writer"), ("S3_LITELLM_WRITER_KEY", "S3_LITELLM_WRITER_SECRET"))

    def test_buckets_follow_env(self):
        b = render.s3_buckets(env(LAB_AUDIT_LOCK_MODE="COMPLIANCE", LAB_AUDIT_RETENTION_DAYS="30"))
        self.assertEqual(b["audit-raw"]["lock"], {"Mode": "COMPLIANCE", "Days": 30})
        self.assertEqual(b["audit-raw"]["lifecycle"][0]["Expiration"], {"Days": 31})
        self.assertEqual(b["audit-access"]["lock"]["Days"], 400)
        self.assertIsNone(b["audit-derived"]["lock"])
        self.assertFalse(b["pg-backup"]["versioning"])

    def test_no_user_may_delete_or_bypass_audit_records(self):
        for user, doc in render.s3_policies().items():
            for st in doc["Statement"]:
                touches_audit = any(r.startswith(("arn:aws:s3:::audit-raw", "arn:aws:s3:::audit-access"))
                                    for r in st["Resource"])
                if touches_audit:
                    for action in st["Action"]:
                        self.assertNotIn(action, ("s3:*", "s3:DeleteObject", "s3:DeleteObjectVersion",
                                                  "s3:BypassGovernanceRetention"), (user, action))

    def test_writer_can_only_append_raw(self):
        doc = render.s3_policies()["litellm-writer"]
        self.assertEqual(doc["Statement"], [{"Effect": "Allow", "Action": ["s3:PutObject"],
                                             "Resource": ["arn:aws:s3:::audit-raw/raw/*"]}])


class DatabaseSettingsTest(unittest.TestCase):
    def test_pooler_is_default(self):
        o = render.helm_overrides(env(), "repo", "tag", "h")
        self.assertEqual(o["db"]["endpoint"], "litellm-pg-pooler-rw")
        self.assertEqual(o["envVars"]["DATABASE_DISABLE_PREPARED_STATEMENTS"], "true")
        self.assertEqual(o["envVars"]["DISABLE_SCHEMA_UPDATE"], "true")
        cfg = render.litellm_config(env(), has_chatgpt=False)
        self.assertEqual(cfg["general_settings"]["database_connection_pool_limit"], 10)

    def test_direct(self):
        e = env(LAB_DB_POOLER="direct")
        o = render.helm_overrides(e, "repo", "tag", "h")
        self.assertEqual(o["db"]["endpoint"], "litellm-pg-rw")
        self.assertNotIn("DATABASE_DISABLE_PREPARED_STATEMENTS", o["envVars"])
        self.assertEqual(render.litellm_config(e, has_chatgpt=False)["general_settings"]
                         ["database_connection_pool_limit"], 5)

    def test_unknown_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            render.db_pooler(env(LAB_DB_POOLER="pgbouncer"))

    def test_secret_type(self):
        m = json.loads(secret_manifest("s", {"username": "u"}, "kubernetes.io/basic-auth"))
        self.assertEqual(m["type"], "kubernetes.io/basic-auth")


class RedisSettingsTest(unittest.TestCase):
    def test_sentinel_only_via_env(self):
        cfg = render.litellm_config(env(), has_chatgpt=False)
        # router_settings に Redis のキーがあると、ルーターが Sentinel を使わない別の接続を作る
        self.assertFalse([k for k in cfg["router_settings"] if k.startswith("redis")])
        self.assertTrue(cfg["general_settings"]["use_redis_transaction_buffer"])
        o = render.helm_overrides(env(), "repo", "tag", "h")
        nodes = json.loads(o["envVars"]["REDIS_SENTINEL_NODES"])
        self.assertEqual(len(nodes), 3)
        self.assertEqual(o["envVars"]["REDIS_SERVICE_NAME"], "mymaster")
        data = render.secrets_for_litellm(env(REDIS_PASSWORD="r", SENTINEL_PASSWORD="s"))
        self.assertNotIn("REDIS_HOST", data)
        self.assertEqual(data["REDIS_SENTINEL_PASSWORD"], "s")


class PricingAndPinningTest(unittest.TestCase):
    def test_price_env_names_are_readable_from_env_file(self):
        self.assertEqual(render.price_env_prefix("review-judge"), "LAB_PRICE_REVIEW_JUDGE")
        self.assertEqual(render.price_env_prefix("gpt-5.6-terra"), "LAB_PRICE_GPT_5_6_TERRA")
        self.assertIn("LAB_PRICE_GPT_5_6_TERRA_IN", parse_env("LAB_PRICE_GPT_5_6_TERRA_IN=3\n"))

    def test_model_prices_default_and_override(self):
        p = render.model_prices(env(), "gpt-5.6-terra", "gpt-5.6-terra")
        self.assertAlmostEqual(p["input_cost_per_token"], 2.0e-6)
        p = render.model_prices(env(LAB_PRICE_REVIEW_JUDGE_IN="1", LAB_PRICE_REVIEW_JUDGE_OUT="2",
                                    LAB_PRICE_REVIEW_JUDGE_CACHE_READ="0.1"), "review-judge", "unknown")
        self.assertAlmostEqual(p["output_cost_per_token"], 2.0e-6)
        self.assertIsNone(render.model_prices(env(), "x", "unknown"))
        cfg = render.litellm_config(env(), has_chatgpt=True)
        terra = next(m for m in cfg["model_list"] if m["model_name"] == "gpt-5.6-terra")
        self.assertIn("model_info", terra)
        self.assertEqual(cfg["litellm_settings"]["callbacks"], ["audit_log.audit_logger_instance"])
        self.assertTrue(cfg["general_settings"]["disable_spend_logs"])

    def test_pin_deployments(self):
        text = "kind: Service\nmetadata: {name: s}\n---\nkind: Deployment\nmetadata: {name: d}\nspec:\n  template:\n    spec: {}\n"
        docs = list(yaml.safe_load_all(render.pin_deployments(text, {"a": "b"})))
        self.assertEqual(docs[1]["spec"]["template"]["spec"]["nodeSelector"], {"a": "b"})
        self.assertNotIn("spec", docs[0])


if __name__ == "__main__":
    unittest.main()
