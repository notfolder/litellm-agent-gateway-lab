"""構築ツールの単体テスト（クラスタや Docker を使わない部分）。./lab test-unit で流す。"""

import base64
import json
import re
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
        self.assertFalse(params["context_review_claude_code"])   # 既定は切り（auto mode に任せる）
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

    def test_context_review_claude_code(self):
        # LAB_CONTEXT_REVIEW_CLAUDE_CODE=true で Claude Code にも文脈あり審査を掛ける（文脈独立審査の有無に関わらず出す）
        cfg = render.litellm_config(env(LAB_CONTEXT_REVIEW_CLAUDE_CODE="true"), has_chatgpt=False)
        self.assertTrue(cfg["guardrails"][0]["litellm_params"]["context_review_claude_code"])
        cfg = render.litellm_config(env(LAB_CONTEXT_REVIEW_CLAUDE_CODE="true", LAB_ISOLATED_REVIEW="false"),
                                    has_chatgpt=False)
        self.assertTrue(cfg["guardrails"][0]["litellm_params"]["context_review_claude_code"])

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


class AwsTargetTest(unittest.TestCase):
    """LAB_TARGET=aws のときの描画（変更仕様書 2026-10-24-AWS構成 2.3 節）。local の振る舞いは上のテストで変わらないこと。"""

    OUT = {"AuroraWriterEndpoint": "pg.cluster-x.ap-northeast-1.rds.amazonaws.com",
           "AuroraSecretName": "litellm-lab/aurora", "ValkeyPrimaryEndpoint": "master.v.cache.amazonaws.com",
           "ValkeyPort": "6379", "ValkeyAuthSecretName": "litellm-lab/valkey-auth",
           "CertificateArn": "arn:aws:acm:ap-northeast-1:1:certificate/abc", "EfsFileSystemId": "fs-1"}

    def aws(self, **extra):
        e = env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="lab.example.test", TYPESAFE_API_KEY="t", **extra)
        e.values["LAB_AWS_BUCKET_PREFIX"] = render.bucket_prefix(e, "111111111111")
        return e

    def test_target_and_urls(self):
        self.assertEqual(env().target, "local")
        self.assertEqual(env().base_url, "http://localhost:8080")
        a = self.aws()
        self.assertTrue(a.is_aws)
        self.assertEqual(a.base_url, "https://lab.example.test")
        self.assertEqual(a.public_host, "lab.example.test")
        self.assertEqual(a.aws_cluster_name, "litellm-lab-eks")
        with self.assertRaises(Exception):
            _ = env(LAB_TARGET="aws").aws_hostname   # ホスト名が無いと止まる

    def test_bucket_names(self):
        self.assertEqual(render.bucket_name(env(), "audit-raw"), "audit-raw")
        self.assertEqual(render.bucket_prefix(env(), "1"), "")
        a = self.aws()
        self.assertEqual(render.bucket_name(a, "audit-raw"), "litellm-lab-111111111111-audit-raw")

    def test_models_are_bedrock_and_jev_is_off(self):
        cfg = render.litellm_config(self.aws(), has_chatgpt=False)
        models = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
        self.assertIn("claude-sonnet-4-5", models)
        self.assertTrue(models["claude-sonnet-4-5"]["model"].startswith("bedrock/jp.anthropic.claude-sonnet-4-5"))
        self.assertEqual(models["claude-sonnet-4-5"]["aws_region_name"], "ap-northeast-1")
        self.assertNotIn("claude-opus-4-6", models)          # 日本内のプロファイルが無いので既定では登録しない
        self.assertNotIn("anthropic/*", models)              # Claude Max のトークン転送は使わない
        self.assertTrue(models["review-judge"]["model"].startswith("bedrock/jp.anthropic.claude-haiku-4-5"))
        self.assertTrue(models["isolated-judge"]["model"].startswith("bedrock/jp.anthropic.claude-haiku-4-5"))
        self.assertFalse(cfg["guardrails"][0]["litellm_params"]["use_jev"])   # Jev の鍵があっても使わない
        # Codex の chatgpt/ はトークンがあれば Mac と同じ
        cfg2 = render.litellm_config(self.aws(), has_chatgpt=True)
        self.assertIn("gpt-5.6-terra", {m["model_name"] for m in cfg2["model_list"]})

    def test_opus_can_be_added_by_env(self):
        cfg = render.litellm_config(self.aws(LAB_BEDROCK_OPUS="global.anthropic.claude-opus-4-6-v1"), has_chatgpt=False)
        self.assertIn("claude-opus-4-6", {m["model_name"] for m in cfg["model_list"]})
        envs = render.claude_code_model_env(self.aws())
        self.assertEqual(envs["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-sonnet-4-5")   # 無ければ Sonnet に落とす
        self.assertEqual(envs["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-4-5")

    def test_helm_overrides(self):
        local = render.helm_overrides(env(), "r", "t", "h")
        self.assertIn("REDIS_SENTINEL_NODES", local["envVars"])
        self.assertEqual(local["db"]["endpoint"], "litellm-pg-pooler-rw")
        a = render.helm_overrides(self.aws(LAB_DB_POOLER="pooler"), "r", "t", "h", self.OUT)
        ev = a["envVars"]
        self.assertNotIn("REDIS_SENTINEL_NODES", ev)
        self.assertEqual((ev["REDIS_HOST"], ev["REDIS_PORT"], ev["REDIS_SSL"]), (self.OUT["ValkeyPrimaryEndpoint"], "6379", "True"))
        self.assertEqual(ev["AUDIT_S3_ENDPOINT"], "")
        self.assertEqual(ev["AUDIT_S3_BUCKET"], "litellm-lab-111111111111-audit-raw")
        self.assertEqual(ev["AUDIT_S3_CREDENTIAL_CHAIN"], "true")
        self.assertNotIn("DATABASE_DISABLE_PREPARED_STATEMENTS", ev)    # AWS は直結のみ
        self.assertEqual(a["db"]["endpoint"], self.OUT["AuroraWriterEndpoint"])
        self.assertIn("sslmode=require", a["db"]["url"])
        self.assertEqual(a["serviceAccount"], {"create": True, "name": "litellm"})
        self.assertEqual(a["volumes"][0]["persistentVolumeClaim"]["claimName"], "chatgpt-token")

    def test_external_secrets(self):
        docs = list(yaml.safe_load_all(render.external_secrets_manifest(self.aws(GOOGLE_OAUTH_CLIENT_ID="g", GOOGLE_OAUTH_CLIENT_SECRET="s"), self.OUT)))
        names = {d["metadata"]["name"] for d in docs}
        self.assertEqual(names, {"litellm-secrets", "litellm-pg-app", "litellm-migrate-db", "audit-viewer-session",
                                 "audit-viewer-oidc", "workspace-mcp"})
        migrate = next(d for d in docs if d["metadata"]["name"] == "litellm-migrate-db")
        url = migrate["spec"]["target"]["template"]["data"]["DATABASE_URL"]
        self.assertIn("sslmode=require", url)
        self.assertIn("urlquery", url)
        self.assertEqual(migrate["spec"]["dataFrom"][0]["extract"]["key"], "litellm-lab/aurora")
        lit = next(d for d in docs if d["metadata"]["name"] == "litellm-secrets")
        self.assertEqual(lit["spec"]["data"][0]["remoteRef"]["key"], "litellm-lab/valkey-auth")
        for d in docs:
            self.assertEqual(d["spec"]["secretStoreRef"]["name"], render.ESO_STORE)

    def test_alb_ingress_order_and_actions(self):
        docs = list(yaml.safe_load_all(render.alb_ingress_manifest(self.aws(), self.OUT, viewer=True)))
        ing = next(d for d in docs if d["kind"] == "Ingress")
        ann = ing["metadata"]["annotations"]
        self.assertEqual(json.loads(ann["alb.ingress.kubernetes.io/actions.deny-passthrough"])["fixedResponseConfig"]["statusCode"], "403")
        self.assertEqual(json.loads(ann["alb.ingress.kubernetes.io/actions.mcp-get-not-allowed"])["fixedResponseConfig"]["statusCode"], "405")
        paths = [p["path"] for p in ing["spec"]["rules"][0]["http"]["paths"]]
        # 素通しの拒否 → MCP の GET → 検索アプリ → 残りは LiteLLM、の順
        self.assertIn("/anthropic/*", paths)
        self.assertLess(paths.index("/anthropic/*"), paths.index("/*/mcp"))
        self.assertLess(paths.index("/*/mcp"), paths.index("/lab-audit/*"))
        self.assertEqual(paths[-1], "/*")
        self.assertTrue(all(f"/{p}/*" in paths for p in render.PASSTHROUGH_PROVIDERS))
        params = next(d for d in docs if d["kind"] == "IngressClassParams")
        self.assertEqual(params["spec"]["certificateARNs"], [self.OUT["CertificateArn"]])
        without = list(yaml.safe_load_all(render.alb_ingress_manifest(self.aws(), self.OUT, viewer=False)))
        self.assertNotIn("/lab-audit/*", [p["path"] for p in next(d for d in without if d["kind"] == "Ingress")["spec"]["rules"][0]["http"]["paths"]])

    def test_traefik_regex_lists_the_same_providers(self):
        # Traefik（k8s/ingress.yaml）の正規表現と ALB の一覧がずれない
        text = (Path(__file__).resolve().parents[2] / "k8s" / "ingress.yaml").read_text()
        import re
        m = re.search(r"PathRegexp\(`\^/\((.+?)\)\(/\|\$\)`\)", text)
        self.assertIsNotNone(m)
        traefik = {p.replace("\\.", ".") for p in m.group(1).split("|")}
        self.assertEqual(traefik, set(render.PASSTHROUGH_PROVIDERS))

    def test_client_files_point_to_https_and_pin_models(self):
        files = render.client_files(self.aws(LAB_USER1_EMAIL="a@example.com", LAB_KEY_USER1_CLAUDE="sk-c", LAB_KEY_USER1_CODEX="sk-x"),
                                    "sk-c", "sk-x", Path("/tmp/c"), Path("/tmp/n"))
        self.assertIn("export ANTHROPIC_BASE_URL='https://lab.example.test'", files["claude.env"])
        self.assertIn("export ANTHROPIC_DEFAULT_SONNET_MODEL='claude-sonnet-4-5'", files["claude.env"])
        self.assertIn("lab.example.test", files["claude-settings.json"])
        self.assertIn('base_url = "https://lab.example.test/v1"', files["codex-config.toml"])
        local = render.client_files(env(), "k", "k", Path("/tmp/c"), Path("/tmp/n"))
        self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", local["claude.env"])

    def test_initial_contracts(self):
        c = render.initial_contracts(self.aws())
        ids = {x["id"]: x for x in c["contracts"]}
        self.assertEqual(ids["bedrock"]["kind"], "invoice")
        self.assertIn("review-judge", ids["bedrock"]["models"])
        self.assertEqual(ids["chatgpt-plus"]["models"], ["gpt-5.6-terra"])
        self.assertIn("claude-max", {x["id"] for x in render.initial_contracts(env())["contracts"]})

    def test_nodepool_and_efs_and_service_accounts(self):
        np = yaml.safe_load(render.nodepool_manifest(self.aws()))
        reqs = {r["key"]: r["values"] for r in np["spec"]["template"]["spec"]["requirements"]}
        self.assertEqual(reqs["kubernetes.io/arch"], ["arm64"])
        efs = list(yaml.safe_load_all(render.efs_manifests(self.OUT)))
        self.assertEqual(efs[0]["parameters"]["fileSystemId"], "fs-1")
        self.assertEqual(efs[1]["spec"]["accessModes"], ["ReadWriteMany"])
        sas = list(yaml.safe_load_all(render.service_accounts_manifest()))
        self.assertEqual({d["metadata"]["name"] for d in sas}, {"audit-compactor", "audit-viewer"})

    def test_retained_bucket_names(self):
        """./lab down の後始末で消す S3 のバケット: 接頭辞付きの 3 つ。pg-backup は AWS では作らないので含めない"""
        from labtool import aws
        names = aws.retained_bucket_names(self.aws(), "123456789012")
        self.assertEqual(names, [f"litellm-lab-123456789012-{b}" for b in ("audit-raw", "audit-access", "audit-derived")])

    def test_operator_principal_arn(self):
        from labtool import aws
        sso = {"Account": "1", "Arn": "arn:aws:sts::1:assumed-role/AWSReservedSSO_Admin_x/me"}
        # SSO のロールはパス付きの ARN を IAM から引く（パス無しは EKS が invalid principal と返す）
        self.assertEqual(aws.operator_principal_arn(sso, lambda r: f"arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/{r}"),
                         "arn:aws:iam::1:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_Admin_x")
        self.assertEqual(aws.operator_principal_arn(sso), "arn:aws:iam::1:role/AWSReservedSSO_Admin_x")
        self.assertEqual(aws.operator_principal_arn({"Account": "1", "Arn": "arn:aws:iam::1:user/me"}), "arn:aws:iam::1:user/me")


class VmTargetTest(unittest.TestCase):
    """LAB_TARGET=aws-ec2 / gcp-vm（VM 3 台の k3s とクラウドのバケット）のときの描画
    （変更仕様書 .history/2026-10-30-VM環境の共通化とGCP追加/ 2 章・4 章、変更設計書 2 章・8 章）。local と aws の振る舞いは上のテストで変わらないこと。"""

    def vm(self, target="aws-ec2", **extra):
        nodes = "i-1,i-2,i-3" if target == "aws-ec2" else "asia-northeast1-b/n1,asia-northeast1-c/n2,asia-northeast1-b/n3"
        base = dict(LAB_TARGET=target, LAB_HOSTNAME="lab.example.test", LAB_ACME_EMAIL="ops@example.test",
                    TYPESAFE_API_KEY="t", LAB_GCP_PROJECT="my-project", LAB_JUDGE_ANTHROPIC_API_KEY="sk-judge",
                    LAB_VM_BUCKET="lab-bucket", LAB_VM_NODES=nodes,
                    LAB_VM_ENTRY_IP_ID="eipalloc-1" if target == "aws-ec2" else "litellm-lab-entry")
        return env(**{**base, **extra})

    def test_target_flags_and_urls(self):
        e = self.vm()
        self.assertTrue(e.is_aws_ec2 and e.is_vm and e.on_aws and e.on_cloud and not e.is_aws and not e.is_gcp_vm)
        g = self.vm("gcp-vm")
        self.assertTrue(g.is_gcp_vm and g.is_vm and g.on_cloud and not g.on_aws)
        self.assertFalse(env().on_cloud or env().is_vm)
        self.assertEqual((e.base_url, g.base_url), ("https://lab.example.test", "https://lab.example.test"))
        self.assertEqual(env(LAB_TARGET="aws-ec2", LAB_AWS_HOSTNAME="old.example.test").hostname, "old.example.test")
        self.assertEqual((e.vm_prefix, g.vm_prefix), ("litellm-lab-ec2", "litellm-lab"))
        self.assertEqual((e.vm_cidr, e.vm_disk_gib, e.vm_local_port, e.vm_local_https_port), ("10.60.0.0/16", 40, 16443, 18443))
        # 入口は内部ロードバランサなので、Mac は踏み台経由の転送口に別名で届く（変更設計書 2026-10-31 6 章）
        local = self.vm("gcp-vm", LAB_LOCAL_HOSTNAME="lab-local.example.test")
        self.assertEqual((local.base_url, local.public_host), ("https://lab-local.example.test:18443", "lab-local.example.test:18443"))
        self.assertEqual(render.vm_certificate_names(local), ["lab.example.test", "lab-local.example.test"])
        self.assertEqual(render.vm_certificate_names(e), ["lab.example.test"])
        self.assertEqual(yaml.safe_load(render.vm_certificate_manifest(local))["spec"]["dnsNames"], ["lab.example.test", "lab-local.example.test"])
        self.assertEqual((e.zabbix_enabled, self.vm(LAB_ZABBIX_VM="true").zabbix_enabled, self.vm(LAB_ZABBIX_SERVER="10.0.0.20:10051").zabbix_enabled),
                         (False, True, True))
        self.assertEqual(self.vm(LAB_ZABBIX_VM="true").zabbix_api_url, "http://127.0.0.1:18080/api_jsonrpc.php")
        self.assertEqual(self.vm(LAB_ZABBIX_API_URL="https://z.example.test/api_jsonrpc.php").zabbix_api_url,
                         "https://z.example.test/api_jsonrpc.php")
        self.assertEqual((e.vm_region, g.vm_region, g.gcp_machine_type), ("ap-northeast-1", "asia-northeast1", "c4a-standard-2"))
        # VM の環境は 1 つのバケットのディレクトリ（変更設計書 2026-10-30-既存資源からの構築と出口プロキシ 4.1 節）
        self.assertEqual(render.store_location(e, "audit-raw"), ("lab-bucket", "audit-raw/"))
        self.assertEqual(render.store_location(g, "pg-backup"), ("lab-bucket", "pg-backup/"))
        self.assertEqual(render.store_location(env(), "audit-raw"), ("audit-raw", ""))
        self.assertEqual(render.store_location(env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="x", LAB_AWS_BUCKET_PREFIX="p-"), "audit-raw"),
                         ("p-audit-raw", ""))
        self.assertEqual((e.infra_mode, e.is_provided, env(LAB_TARGET="gcp-vm", LAB_INFRA="provided").is_provided), ("managed", False, True))
        self.assertEqual(e.vm_nodes, ["i-1", "i-2", "i-3"])
        with self.assertRaises(Exception):
            _ = env(LAB_INFRA="other").infra_mode
        with self.assertRaises(Exception):
            _ = env(LAB_TARGET="gcp-vm", LAB_HOSTNAME="x").gcp_project   # プロジェクトが無いと止まる

    def test_models_use_max_token_forwarding_and_api_key_judges(self):
        """Claude Code 用は Mac と同じ anthropic/*（キー無し、Max のトークンの転送）、審査官は Anthropic の API キー。
        ANTHROPIC_API_KEY の名前は Pod に置かない（転送がキーに置き換わらないように）。"""
        for target in ("aws-ec2", "gcp-vm"):
            cfg = render.litellm_config(self.vm(target), has_chatgpt=False)
            models = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
            self.assertEqual(models["anthropic/*"], {"model": "anthropic/*"})
            self.assertEqual(models["review-judge"], {"model": "anthropic/claude-haiku-4-5-20251001",
                                                      "api_key": "os.environ/LAB_JUDGE_ANTHROPIC_API_KEY"})
            self.assertEqual(models["isolated-judge"]["api_key"], "os.environ/LAB_JUDGE_ANTHROPIC_API_KEY")
            self.assertFalse(cfg["guardrails"][0]["litellm_params"]["use_jev"])
            self.assertFalse(any(m["litellm_params"]["model"].startswith("bedrock/") for m in cfg["model_list"]))
            secrets = render.secrets_for_litellm(self.vm(target))
            self.assertEqual(secrets["LAB_JUDGE_ANTHROPIC_API_KEY"], "sk-judge")
            self.assertNotIn("ANTHROPIC_API_KEY", secrets)
            self.assertNotIn("BEDROCK_ACCESS_KEY_ID", secrets)
            contracts = {c["id"]: c for c in render.initial_contracts(self.vm(target))["contracts"]}
            self.assertEqual(set(contracts), {"claude-max", "anthropic-api"})
            self.assertEqual(contracts["anthropic-api"]["models"], ["review-judge", "isolated-judge"])
        self.assertEqual(render.vm_judge_model(self.vm(LAB_VM_JUDGE_MODEL="claude-haiku-9")), "claude-haiku-9")
        self.assertNotIn("LAB_JUDGE_ANTHROPIC_API_KEY", render.secrets_for_litellm(env()))
        # EKS 版は Bedrock のまま
        aws_cfg = render.litellm_config(env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="x.test"), has_chatgpt=False)
        self.assertTrue({m["model_name"]: m["litellm_params"] for m in aws_cfg["model_list"]}["review-judge"]["model"].startswith("bedrock/"))

    def test_helm_overrides_keep_local_shape_but_use_cloud_buckets(self):
        o = render.helm_overrides(self.vm(), "lab-registry:5000/litellm-lab", "t", "h")
        ev = o["envVars"]
        self.assertIn("REDIS_SENTINEL_NODES", ev)                      # Valkey は Mac と同じ Sentinel
        self.assertEqual(o["db"]["endpoint"], "litellm-pg-pooler-rw")  # Pooler も Mac と同じ
        self.assertEqual((ev["AUDIT_S3_ENDPOINT"], ev["AUDIT_S3_REGION"]), ("", "ap-northeast-1"))
        self.assertEqual((ev["AUDIT_S3_BUCKET"], ev["AUDIT_S3_KEY_PREFIX"]), ("lab-bucket", "audit-raw/"))
        self.assertEqual((ev["AUDIT_S3_RETENTION_DAYS"], ev["AUDIT_S3_RETENTION_MODE"]), ("90", "GOVERNANCE"))   # 原本にオブジェクト単位の保持
        self.assertNotIn("AUDIT_S3_CREDENTIAL_CHAIN", ev)              # 静的キー（Secret litellm-secrets）
        self.assertEqual([v["name"] for v in o["volumes"]], ["audit-spool"])   # chatgpt/ の hostPath は無い
        self.assertNotIn("HTTPS_PROXY", ev)                             # 中継が無ければ何も足さない
        self.assertNotIn("AUDIT_S3_RETENTION_DAYS", render.helm_overrides(env(), "r", "t", "h")["envVars"])   # Mac は既定の保持
        g = render.helm_overrides(self.vm("gcp-vm"), "lab-registry:5000/litellm-lab", "t", "h")["envVars"]
        self.assertEqual((g["AUDIT_S3_ENDPOINT"], g["AUDIT_S3_REGION"]), ("https://storage.googleapis.com", "asia-northeast1"))

    def test_pg_backup_values(self):
        local = render.pg_backup_values(env())
        self.assertEqual(local["S3_DEST"], "s3://pg-backup/litellm-pg")
        self.assertEqual(local["S3_ENDPOINT_LINE"], "endpointURL: http://rustfs:9000")
        e = render.pg_backup_values(self.vm())
        self.assertEqual((e["S3_DEST"], e["S3_ENDPOINT_LINE"], e["S3_REGION"]),
                         ("s3://lab-bucket/pg-backup/litellm-pg", "", "ap-northeast-1"))
        g = render.pg_backup_values(self.vm("gcp-vm"))
        self.assertEqual((g["S3_DEST"], g["S3_ENDPOINT_LINE"], g["S3_REGION"]),
                         ("s3://lab-bucket/pg-backup/litellm-pg", "endpointURL: https://storage.googleapis.com", "asia-northeast1"))
        text = render_template(Path(__file__).resolve().parents[2] / "k8s" / "postgres-cnpg.yaml", e)
        store = next(d for d in yaml.safe_load_all(text) if d["kind"] == "ObjectStore")
        self.assertEqual(store["spec"]["configuration"]["destinationPath"], "s3://lab-bucket/pg-backup/litellm-pg")
        self.assertNotIn("endpointURL", store["spec"]["configuration"])
        self.assertNotIn("endpointCA", store["spec"]["configuration"])   # 中継が無ければ CA の参照は無い
    def test_tls_ingressroutes_copy_routes_to_websecure(self):
        text = (Path(__file__).resolve().parents[2] / "k8s" / "ingress.yaml").read_text()
        docs = list(yaml.safe_load_all(render.tls_ingressroutes(text)))
        self.assertEqual([d["kind"] for d in docs], ["IngressRoute"])
        self.assertEqual(docs[0]["metadata"]["name"], "litellm-tls")
        self.assertEqual(docs[0]["spec"]["entryPoints"], ["websecure"])
        self.assertEqual(docs[0]["spec"]["tls"], {"secretName": "litellm-tls"})
        original = next(d for d in yaml.safe_load_all(text) if d["kind"] == "IngressRoute")
        self.assertEqual(docs[0]["spec"]["routes"], original["spec"]["routes"])   # 経路と Middleware は同じ

    def test_issuer_certificate_and_registry(self):
        issuer = yaml.safe_load(render.vm_issuer_manifest(self.vm()))
        self.assertEqual(issuer["spec"]["acme"]["server"], render.ACME_PRODUCTION)
        self.assertEqual(issuer["spec"]["acme"]["solvers"][0]["dns01"]["cloudflare"]["apiTokenSecretRef"]["name"],
                         "cloudflare-api-token")
        self.assertEqual(yaml.safe_load(render.vm_issuer_manifest(self.vm(LAB_ACME_STAGING="true")))["spec"]["acme"]["server"],
                         render.ACME_STAGING)
        with self.assertRaises(ValueError):
            render.vm_issuer_manifest(env(LAB_TARGET="aws-ec2", LAB_HOSTNAME="x.test"))
        cert = yaml.safe_load(render.vm_certificate_manifest(self.vm()))
        self.assertEqual((cert["spec"]["dnsNames"], cert["spec"]["secretName"]), (["lab.example.test"], "litellm-tls"))
        docs = list(yaml.safe_load_all(render.vm_registry_manifest(self.vm("gcp-vm"), "lab-bucket", 30500)))
        svc = next(d for d in docs if d["kind"] == "Service")
        self.assertEqual((svc["spec"]["type"], svc["spec"]["ports"][0]["nodePort"]), ("NodePort", 30500))
        dep = next(d for d in docs if d["kind"] == "Deployment")
        envs = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        # GCP は GCS ドライバ（均一アクセスのバケットは S3 ドライバの ACL を拒む。2026-10-31）。置き場は 1 バケットの images/ ディレクトリ
        self.assertEqual((envs["REGISTRY_STORAGE"], envs["REGISTRY_STORAGE_GCS_BUCKET"]), ("gcs", "lab-bucket"))
        self.assertEqual((envs["REGISTRY_STORAGE_GCS_ROOTDIRECTORY"], envs["REGISTRY_STORAGE_GCS_KEYFILE"]),
                         ("/images/registry", "/etc/registry-gcs/key.json"))
        self.assertEqual(dep["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"], render.VM_REGISTRY_GCS_SECRET)
        self.assertEqual(dep["spec"]["replicas"], 2)
        adocs = list(yaml.safe_load_all(render.vm_registry_manifest(self.vm(), "lab-bucket", 30500)))
        aenvs = {e["name"]: e["value"] for e in next(d for d in adocs if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual((aenvs["REGISTRY_STORAGE"], aenvs["REGISTRY_STORAGE_S3_BUCKET"], aenvs["REGISTRY_STORAGE_S3_ROOTDIRECTORY"]),
                         ("s3", "lab-bucket", "/images/registry"))
        # 静的キーは .env の LAB_VM_S3_*（Mac の S3_* とは別の名前。同じ .env で切り替えても混ざらない）
        secret = render.vm_registry_secret(self.vm(LAB_VM_S3_REGISTRY_KEY="AKIAREG", LAB_VM_S3_REGISTRY_SECRET="sr",
                                                   S3_REGISTRY_KEY="mac", LAB_REGISTRY_HTTP_SECRET="h"))
        self.assertEqual(render.vm_registry_secret(self.vm("gcp-vm", LAB_REGISTRY_HTTP_SECRET="h")), {"REGISTRY_HTTP_SECRET": "h"})
        self.assertEqual(render.s3_env_names("litellm-writer", env()), ("S3_LITELLM_WRITER_KEY", "S3_LITELLM_WRITER_SECRET"))
        self.assertEqual(render.s3_env_names("litellm-writer", self.vm()), ("LAB_VM_S3_LITELLM_WRITER_KEY", "LAB_VM_S3_LITELLM_WRITER_SECRET"))
        self.assertEqual(secret, {"REGISTRY_STORAGE_S3_ACCESSKEY": "AKIAREG", "REGISTRY_STORAGE_S3_SECRETKEY": "sr",
                                  "REGISTRY_HTTP_SECRET": "h"})

    def test_failover_scenarios(self):
        from labtool import failover
        self.assertEqual(failover.scenarios_for(self.vm()), [*failover.SCENARIOS, "entry-node"])
        self.assertEqual(failover.scenarios_for(self.vm("gcp-vm")), [*failover.SCENARIOS, "entry-node"])
        self.assertEqual(failover.scenarios_for(env()), failover.SCENARIOS)
        self.assertEqual(failover.scenarios_for(env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="x")), failover.SCENARIOS_AWS)
        # 入口は内部ロードバランサ（Traefik の台を止める場面）。目安は両クラウド 60 秒（変更仕様書 2026-10-31 R22）
        self.assertEqual(failover.LIMITS["entry-node"], 60)
        # GCP だけ、台を止める場面の目安を広げる（2026-10-30 の判断 2A）
        gcp = VmTargetTest().vm("gcp-vm")
        self.assertEqual((failover.limit_for(gcp, "entry-node"), failover.limit_for(gcp, "pg-node"), failover.limit_for(gcp, "pg-pod")),
                         (60, 180, 60))
        ec2 = VmTargetTest().vm("aws-ec2")
        self.assertEqual((failover.limit_for(ec2, "entry-node"), failover.limit_for(ec2, "pg-node")), (60, 120))
        self.assertEqual(failover.SCENARIO_ALIASES["eip-node"], "entry-node")   # EC2 版の名前も受ける

    def test_k3s_version_rules(self):
        """k3s の版上げ（./lab upgrade-k8s）: 版の形、マイナー版を 1 つずつ、下げない。"""
        from labtool import vm
        from labtool.core import LabError
        self.assertEqual(vm.parse_k3s_version("v1.35.5+k3s1"), (1, 35, 5))
        self.assertEqual(vm.normalize_k3s_version("v1.36.1-k3s1"), "v1.36.1+k3s1")
        self.assertEqual(vm.k3s_install_version("rancher/k3s:v1.35.5-k3s1"), "v1.35.5+k3s1")
        vm.check_upgrade_step("v1.35.5+k3s1", "v1.36.1+k3s1")    # 次のマイナー版
        vm.check_upgrade_step("v1.35.5+k3s1", "v1.35.7+k3s1")    # 同じマイナー版のパッチ
        for bad in ("v1.37.0+k3s1", "v1.35.4+k3s1", "v1.34.9+k3s1", "v2.0.0+k3s1"):
            with self.assertRaises(LabError):
                vm.check_upgrade_step("v1.35.5+k3s1", bad)
        with self.assertRaises(LabError):
            vm.parse_k3s_version("1.36.1")

    def test_k3s_upgrade_order_puts_entry_holder_last(self):
        from labtool import vm
        ids = ["i-1", "i-2", "i-3"]
        self.assertEqual(vm.upgrade_order(ids, "i-2"), ["i-1", "i-3", "i-2"])
        self.assertEqual(vm.upgrade_order(ids, "i-2", forward_target="i-1"), ["i-3", "i-1", "i-2"])
        self.assertEqual(vm.upgrade_order(ids, "i-2", forward_target="i-2"), ["i-1", "i-3", "i-2"])
        self.assertEqual(vm.upgrade_order(ids, None), ids)

    def test_operator_cidr(self):
        """22 を開ける送信元: .env で固定、無ければ外部に問い合わせた IPv4 の /32。IPv6 や失敗は止める。"""
        from labtool import vm
        from labtool.core import LabError
        self.assertEqual(vm.operator_cidr(self.vm(LAB_SSH_ALLOW_CIDR="203.0.113.5/32")), "203.0.113.5/32")
        self.assertEqual(vm.operator_cidr(self.vm(LAB_SSH_ALLOW_CIDR="203.0.113.5")), "203.0.113.5/32")
        self.assertEqual(vm.operator_cidr(self.vm(), lookup=lambda: "198.51.100.7\n"), "198.51.100.7/32")
        for bad in ("2001:db8::1", "", "not-an-ip"):
            with self.assertRaises(LabError):
                vm.operator_cidr(self.vm(), lookup=lambda: bad)

    def test_env_example_has_vm_items_and_no_ec2_leftovers(self):
        text = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
        for key in ("LAB_HOSTNAME", "LAB_LOCAL_HOSTNAME", "LAB_VM_CIDR", "LAB_VM_LOCAL_HTTPS_PORT", "LAB_SSH_ALLOW_CIDR",
                    "LAB_JUDGE_ANTHROPIC_API_KEY", "LAB_GCP_PROJECT", "LAB_GCP_MACHINE_TYPE", "LAB_INFRA", "LAB_VM_NODES",
                    "LAB_VM_BUCKET", "LAB_VM_ENTRY_IP_ID", "LAB_VM_BASTION_HOST", "LAB_VM_BASTION_CIDR",
                    "LAB_VM_SSH_KEY", "LAB_VM_S3_LITELLM_WRITER_KEY", "LAB_EGRESS_PROXY", "LAB_EGRESS_CA_FILE",
                    "LAB_EGRESS_PROXY_MAC", "LAB_EGRESS_PROXY_VM", "LAB_VM_ARCH", "LAB_ZABBIX_VM", "LAB_ZABBIX_SERVER",
                    "LAB_ZABBIX_API_URL", "LAB_ZABBIX_API_TOKEN", "LAB_ZABBIX_LOCAL_PORT"):
            self.assertIn(f"\n{key}=", text)
        # 浮動 IP と公開の入口は無くなった（2026-10-31）
        for old in ("LAB_EC2_VPC_CIDR", "LAB_EC2_DISK_GIB", "LAB_EC2_LOCAL_PORT", "LAB_EC2_KEEP_EIP", "LAB_AWS_HOSTNAME=",
                    "LAB_VM_KEEP_ENTRY_IP", "LAB_WEB_ALLOW_CIDR"):
            self.assertNotIn(old, text)


class CloudLayerTest(unittest.TestCase):
    """クラウド固有層（labtool/clouds/）: 2 つの実装が同じ関数の組を持つこと、ネットワークの計画、VM の初期化の描画
    （変更設計書 3 章〜6 章）。クラウドの API は呼ばない。"""

    def test_provided_env_requirements(self):
        """LAB_INFRA=provided は .env の既存資源の項目（VM 3 台、バケット、浮動 IP、静的キー、中継の CA）がそろっていないと止まる。"""
        from labtool import vm
        from labtool.core import LabError
        e = VmTargetTest().vm("gcp-vm", LAB_INFRA="provided")
        with self.assertRaises(LabError) as cm:
            vm.require_provided_env(e)
        self.assertIn("LAB_VM_S3_LITELLM_WRITER_KEY", str(cm.exception))
        self.assertIn("LAB_VM_S3_OPERATOR_KEY", str(cm.exception))   # GCP は操作者の HMAC キーも必須
        self.assertIn(render.GCS_REGISTRY_KEY_ENV, str(cm.exception))   # GCP の registry は JSON キー
        self.assertIn("LAB_VM_BASTION_HOST", str(cm.exception))   # 踏み台（管理系の経路と Mac の転送）も必須（2026-10-31）
        for user in ("litellm-writer", "audit-compactor", "audit-viewer", "pg-backup", "operator"):
            for name in render.s3_env_names(user, e):
                e.values[name] = "x"
        e.values[render.GCS_REGISTRY_KEY_ENV] = "e30="
        e.values["LAB_VM_BASTION_HOST"], e.values["LAB_VM_BASTION_CIDR"] = "203.0.113.9", "10.60.0.20/32"
        vm.require_provided_env(e)
        e.values["LAB_ZABBIX_SERVER"] = "10.60.0.20:10051"
        with self.assertRaises(LabError):
            vm.require_provided_env(e)   # 既存の Zabbix サーバーを使うなら API の URL とトークンが要る
        e.values["LAB_ZABBIX_API_URL"], e.values["LAB_ZABBIX_API_TOKEN"] = "http://z/api_jsonrpc.php", "tok"
        vm.require_provided_env(e)
        e.values["LAB_EGRESS_PROXY"] = "http://10.0.0.20:3128"
        with self.assertRaises(LabError):
            vm.require_provided_env(e)   # 中継があれば CA が要る
        a = VmTargetTest().vm("aws-ec2", LAB_INFRA="provided", LAB_VM_NODES="i-1,i-2")
        with self.assertRaises(LabError) as cm:
            vm.require_provided_env(a)
        self.assertIn("LAB_VM_NODES", str(cm.exception))
    def test_admin_cidrs(self):
        """VM の 22 を通す送信元: 踏み台の CIDR ＋ managed では操作者の IP。provided は踏み台だけ（変更設計書 2026-10-31 4.1 節）。"""
        from labtool import vm
        env = VmTargetTest().vm("gcp-vm", LAB_SSH_ALLOW_CIDR="203.0.113.5", LAB_VM_BASTION_CIDR="10.60.0.20")
        self.assertEqual(vm.admin_cidrs(env), ["10.60.0.20/32", "203.0.113.5/32"])
        prov = VmTargetTest().vm("gcp-vm", LAB_INFRA="provided", LAB_SSH_ALLOW_CIDR="203.0.113.5", LAB_VM_BASTION_CIDR="10.1.0.0/24")
        self.assertEqual(vm.admin_cidrs(prov), ["10.1.0.0/24"])
        infra = self.infra("gcp-vm")
        infra.bastion_cidr = "10.60.0.21/32"
        self.assertEqual(vm.admin_cidrs(env, infra), ["10.60.0.21/32", "203.0.113.5/32"])   # 基盤の値が .env より優先

    def test_cloudflare_zone_candidates(self):
        """A レコードを作るときのゾーン名の候補: ホスト名から長い順に、TLD だけは除く。"""
        from labtool import vm
        self.assertEqual(vm.zone_candidates("litellm-lab.example.com"), ["litellm-lab.example.com", "example.com"])
        self.assertEqual(vm.zone_candidates("a.b.example.co.jp"), ["a.b.example.co.jp", "b.example.co.jp", "example.co.jp", "co.jp"])

    def test_gcp_zone_assignment(self):
        """GCP のゾーンの割り振り: 3 つあれば 1 台ずつ、2 つしかなければ繰り返す（東京の C4A は b と c だけ。2026-10-30 に判明）、無ければ止まる。"""
        from labtool.clouds import gcp
        from labtool.core import LabError
        self.assertEqual(gcp.assign_zones(["asia-northeast1-a", "asia-northeast1-b", "asia-northeast1-c"]),
                         ["asia-northeast1-a", "asia-northeast1-b", "asia-northeast1-c"])
        self.assertEqual(gcp.assign_zones(["asia-northeast1-b", "asia-northeast1-c"]),
                         ["asia-northeast1-b", "asia-northeast1-c", "asia-northeast1-b"])
        with self.assertRaises(LabError):
            gcp.assign_zones([])

    def infra(self, target):
        from labtool import clouds
        plan = clouds.network_plan("10.60.0.0/16", subnets_per_node=(target == "aws-ec2"))
        nodes = [clouds.Node(id=f"id-{n['index']}", index=n["index"], private_ip=n["private_ip"], public_ip="", zone=f"z{n['index']}")
                 for n in plan["nodes"]]
        return clouds.Infra(nodes=nodes, entry_ip=plan["lb_ips"][0],
                            entry_ip_id="arn:aws:elasticloadbalancing:r:1:loadbalancer/net/lb/1" if target == "aws-ec2" else "litellm-lab-lb",
                            bucket="lab-bucket", region="r", bastion_host="203.0.113.20", bastion_cidr="10.60.0.20/32")

    def test_both_clouds_implement_the_same_functions(self):
        import inspect
        from labtool import clouds
        from labtool.clouds import aws_ec2, gcp
        for name in clouds.FUNCTIONS:
            a, g = getattr(aws_ec2, name, None), getattr(gcp, name, None)
            self.assertTrue(callable(a) and callable(g), f"{name} が両方にありません")
            self.assertEqual(str(inspect.signature(a)), str(inspect.signature(g)), f"{name} の引き数が違います")
        self.assertIs(clouds.for_env(VmTargetTest().vm()), aws_ec2)
        self.assertIs(clouds.for_env(VmTargetTest().vm("gcp-vm")), gcp)
        with self.assertRaises(Exception):
            clouds.for_env(env())

    def test_network_plans(self):
        from labtool import clouds
        aws_plan = clouds.network_plan("10.60.0.0/16", subnets_per_node=True)
        self.assertEqual(aws_plan["subnets"], ["10.60.0.0/24", "10.60.1.0/24", "10.60.2.0/24"])
        self.assertEqual(aws_plan["nodes"][0], {"index": 1, "subnet": "10.60.0.0/24", "private_ip": "10.60.0.10"})
        self.assertEqual(aws_plan["nodes"][2]["private_ip"], "10.60.2.10")
        # 内部ロードバランサの固定の内部 IP は各サブネットの .5（AWS は 3 つ、GCP は 1 つ）。浮動 IP 用の 2 つ目の IP は無い
        self.assertEqual(aws_plan["lb_ips"], ["10.60.0.5", "10.60.1.5", "10.60.2.5"])
        gcp_plan = clouds.network_plan("10.60.0.0/16", subnets_per_node=False)
        self.assertEqual(gcp_plan["subnets"], ["10.60.0.0/24"])
        self.assertEqual([n["private_ip"] for n in gcp_plan["nodes"]], ["10.60.0.10", "10.60.0.11", "10.60.0.12"])
        self.assertEqual(gcp_plan["lb_ips"], ["10.60.0.5"])
        self.assertFalse(any("secondary_ip" in n for n in gcp_plan["nodes"] + aws_plan["nodes"]))

    def test_single_bucket_layout_and_policies(self):
        """1 バケットのディレクトリ構成（変更設計書 4 章）: ライフサイクル、IAM ユーザーの権限の写し、GCS のバインディングの写し。"""
        from labtool import clouds, objectstore
        from labtool.clouds import aws_ec2, gcp
        e = VmTargetTest().vm()
        rules = {r["ID"]: r for r in objectstore.vm_lifecycle_rules(e)}
        self.assertEqual(rules["audit-raw-all"]["Filter"], {"Prefix": "audit-raw/"})
        self.assertEqual((rules["audit-raw-all"]["Expiration"]["Days"], rules["audit-access-all"]["Expiration"]["Days"]), (91, 401))
        self.assertEqual(rules["audit-derived-meta"]["Filter"], {"Prefix": "audit-derived/meta/"})
        self.assertEqual(rules["pg-backup-versions"]["NoncurrentVersionExpiration"], {"NoncurrentDays": 1})
        self.assertNotIn("Expiration", rules["images-versions"])
        self.assertEqual((aws_ec2.s3_endpoint(e), gcp.s3_endpoint(e)), ("", "https://storage.googleapis.com"))
        # AWS: 1 バケットのディレクトリの ARN。一覧はバケット全体。保持を付けて書く利用者に PutObjectRetention。全員に明示的な拒否
        pol = clouds.s3_user_policies(e, "lab-bucket")
        self.assertEqual(set(pol), set(clouds.STATIC_KEY_USERS))
        writer = pol["litellm-writer"]["Statement"]
        self.assertEqual(writer[0]["Resource"], ["arn:aws:s3:::lab-bucket/audit-raw/raw/*"])
        self.assertIn({"Effect": "Allow", "Action": ["s3:PutObjectRetention"], "Resource": ["arn:aws:s3:::lab-bucket/audit-raw/raw/*"]}, writer)
        self.assertFalse(any("s3:ListBucket" in st["Action"] for st in writer if st["Effect"] == "Allow"))
        compactor = pol["audit-compactor"]["Statement"]
        self.assertTrue(any(st["Action"][0] == "s3:ListBucket" and st["Resource"] == ["arn:aws:s3:::lab-bucket"] for st in compactor))
        self.assertEqual(pol["registry"]["Statement"][0]["Resource"], ["arn:aws:s3:::lab-bucket/images/*"])
        denies = [st for st in pol["pg-backup"]["Statement"] if st["Effect"] == "Deny"]
        self.assertEqual(len(denies), 2)
        self.assertIn("arn:aws:s3:::lab-bucket/audit-raw/*", denies[0]["Resource"])
        self.assertIn("s3:BypassGovernanceRetention", denies[0]["Action"])
        self.assertIn("s3:PutBucketObjectLockConfiguration", denies[1]["Action"])
        # GCP: 条件付きバインディング。原本と閲覧記録は追記＋保持の writer、上書きのあるディレクトリは objectUser、一覧は lister
        b = clouds.gcs_bindings(e, "lab-bucket")
        self.assertIn({"user": "litellm-writer", "role": clouds.GCS_ROLE_WRITER, "prefix": "audit-raw/raw/"}, b)
        self.assertIn({"user": "audit-viewer", "role": clouds.GCS_ROLE_WRITER, "prefix": "audit-access/"}, b)
        self.assertIn({"user": "audit-compactor", "role": "roles/storage.objectUser", "prefix": "audit-derived/"}, b)
        self.assertIn({"user": "audit-compactor", "role": clouds.GCS_ROLE_LISTER, "prefix": ""}, b)
        self.assertIn({"user": "audit-viewer", "role": "roles/storage.objectViewer", "prefix": "audit-raw/"}, b)
        self.assertIn({"user": "pg-backup", "role": "roles/storage.objectAdmin", "prefix": "pg-backup/"}, b)
        self.assertIn({"user": "registry", "role": "roles/storage.objectAdmin", "prefix": "images/"}, b)
        self.assertIn({"user": "operator", "role": "roles/storage.objectAdmin", "prefix": ""}, b)
        self.assertFalse(any(x["user"] == "litellm-writer" and x["role"] != clouds.GCS_ROLE_WRITER for x in b))   # 書き込み用は読めない
        self.assertEqual(gcp._custom_role_name(e, clouds.GCS_ROLE_WRITER), "projects/my-project/roles/litellm_lab_ec2_writer")
        gr = gcp._lifecycle_rules([rules["audit-raw-all"]])
        self.assertEqual(gr, [{"action": {"type": "Delete"}, "condition": {"age": 91, "isLive": True, "matchesPrefix": ["audit-raw/"]}},
                              {"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 91, "isLive": False,
                                                                           "matchesPrefix": ["audit-raw/"]}}])
        # 書く側の保持（自己確認用）
        args = objectstore.retention_put_args(e, "audit-raw")
        self.assertEqual(args["ObjectLockMode"], "GOVERNANCE")
        self.assertEqual(objectstore.retention_put_args(e, "pg-backup"), {})
        self.assertEqual(objectstore.retention_put_args(env(), "audit-raw"), {})
    def test_node_init_rendering_for_both_clouds(self):
        """SSH で流す初期化スクリプト（変更設計書 2026-10-31 4 章）: トークンと版、中継と CA、nftables の集合、Zabbix agent、
        keepalived とクラウドの API が無いこと、置き換え漏れ無し。"""
        from labtool import vm
        from labtool.clouds import aws_ec2, gcp
        values = {"K3S_VERSION": "v1.35.5+k3s1", "K3S_TOKEN": "tok", "SSH_USER": "ubuntu",
                  "EGRESS_PROXY": "http://10.60.0.20:3128", "EGRESS_NO_PROXY": "127.0.0.1,10.60.0.0/16", "EGRESS_CA_B64": "Q0E="}
        for target, cloud in (("aws-ec2", aws_ec2), ("gcp-vm", gcp)):
            e = VmTargetTest().vm(target, LAB_SSH_ALLOW_CIDR="203.0.113.5", LAB_EGRESS_PROXY="http://10.60.0.20:3128",
                                  LAB_ZABBIX_SERVER="10.60.0.20:10051")
            infra = self.infra(target)
            node = infra.nodes[2]
            fw = vm.firewall_values(e, infra)
            self.assertEqual(fw["NODE_IPS"], ",".join(n.private_ip for n in infra.nodes))
            self.assertEqual((fw["VPC_CIDR"], fw["LB_IP"], fw["POD_CIDR"]), ("10.60.0.0/16", infra.entry_ip, "10.42.0.0/16"))
            self.assertEqual(fw["ADMIN_CIDRS"], "10.60.0.20/32,203.0.113.5/32")
            self.assertEqual(fw["HC_RANGES"], ",".join(cloud.health_check_ranges(e)))
            for needle in ("169.254.169.254", "10.60.0.20"):   # メタデータと中継（＝踏み台）は直接届く
                self.assertIn(needle, fw["EGRESS_CIDRS"])
            text = vm.render_node_init(node, infra.nodes, "ssh-ed25519 AAAA test", {**values, **fw, **vm.zabbix_values(e, node)})
            self.assertEqual(re.findall(r"@@[A-Z_0-9]+@@", text), [])
            for needle in ('NODE_INDEX="3"', "ssh-ed25519 AAAA test", "/etc/rancher/k3s/config.yaml", "cluster-init: true",
                           "sh -s - server\n", 'K3S_VERSION="v1.35.5+k3s1"', 'K3S_TOKEN="tok"',
                           'EGRESS_PROXY="http://10.60.0.20:3128"', "update-ca-certificates", "/etc/systemd/system/k3s.service.env",
                           "docker.io", "/var/lib/lab/ready", "table inet lab", "hook prerouting priority -150",
                           "tcp dport 22 ip saddr @admins accept", "tcp dport { 80, 443 } ip saddr @hc accept",
                           "udp dport 8472 ip saddr @nodes accept", "add table inet lab", "delete table inet lab",
                           'ZABBIX_SERVER="10.60.0.20:10051"', f'ZABBIX_HOSTNAME="{vm.zabbix_hostname(node)}"', "zabbix-agent2",
                           'HC_RANGES="' + fw["HC_RANGES"] + '"'):
                self.assertIn(needle, text, f"{target}: {needle}")
            for absent in ("keepalived", "lab_take_entry_ip", "addAccessConfig", "associate-address", "lab-egress.service",
                           "--cluster-init", "gcloud ", "aws ec2"):
                self.assertNotIn(absent, text, f"{target}: {absent} が残っている")
            self.assertNotIn("\nflush ruleset", text)   # 全部を消すと kube-proxy の規則まで消える（説明文には書いてある）
            self.assertEqual(fw["EGRESS_CIDRS"].count("10.60.0.20"), 1)   # 中継と踏み台が同じ IP なら 1 つ
            self.assertNotIn("ssm", text.lower())   # Systems Manager は使わない
        ginfra = self.infra("gcp-vm")
        with self.assertRaises(ValueError):
            vm.render_node_init(ginfra.nodes[0], ginfra.nodes, "k", script="x @@UNKNOWN@@")
        # 中継の値: 中継が無ければ空、あれば除外に VM の CIDR と台の IP。無ければ nftables の外向きは遮断しない
        e = VmTargetTest().vm("gcp-vm")
        self.assertEqual(vm.egress_values(e, ginfra), {"EGRESS_PROXY": "", "EGRESS_NO_PROXY": "", "EGRESS_CA_B64": ""})
        plain = vm.render_node_init(ginfra.nodes[0], ginfra.nodes, "k", {**vm.firewall_values(e, ginfra)})
        self.assertIn('EGRESS_PROXY=""', plain)
        self.assertIn('EGRESS_ELEMS="0.0.0.0/0"', plain)
        # 中継（踏み台）の初期化: nftables、Zabbix サーバー（LAB_ZABBIX_VM）
        proxy_text = vm.render_proxy_init(VmTargetTest().vm("gcp-vm", LAB_SSH_ALLOW_CIDR="203.0.113.5", LAB_ZABBIX_VM="true",
                                                            LAB_EGRESS_PROXY_VM="true"))
        self.assertEqual(re.findall(r"@@[A-Z_0-9]+@@", proxy_text), [])
        for needle in ("mitmdump --mode regular", 'ZABBIX="true"', "zabbix-server-pgsql:ubuntu-7.0-latest", '127.0.0.1:${ZABBIX_WEB_PORT}:8080',
                       'ADMIN_CIDRS="203.0.113.5/32"', "tcp dport { ${PORT}, 10051 } ip saddr @internal accept"):
            self.assertIn(needle, proxy_text)
        self.assertIn('ZABBIX="false"', vm.render_proxy_init(VmTargetTest().vm("gcp-vm", LAB_SSH_ALLOW_CIDR="203.0.113.5")))

    def test_admin_only_and_network_policies(self):
        """管理系の経路は踏み台からだけ（変更設計書 2026-10-31 5 章）: Middleware と経路、既存の経路への差し込み、NetworkPolicy。"""
        e = VmTargetTest().vm("gcp-vm", LAB_VM_BASTION_CIDR="10.60.0.20/32")
        docs = list(yaml.safe_load_all(render.admin_only_manifest(e)))
        self.assertEqual([d["kind"] for d in docs], ["Middleware", "IngressRoute"])
        self.assertEqual(docs[0]["spec"]["ipAllowList"]["sourceRange"], ["10.60.0.20/32"])
        route = docs[1]["spec"]["routes"][0]
        self.assertEqual((route["priority"], route["middlewares"]), (95, [{"name": "admin-only"}]))
        for p in ("/ui", "/login", "/sso", "/metrics"):
            self.assertIn(f"PathPrefix(`{p}`)", route["match"])
        self.assertNotIn("/v1", route["match"])
        # 踏み台が無ければ誰にも開けない
        none = list(yaml.safe_load_all(render.admin_only_manifest(VmTargetTest().vm("gcp-vm"))))[0]
        self.assertEqual(none["spec"]["ipAllowList"]["sourceRange"], ["127.0.0.255/32"])
        # 検索アプリの経路に差し込む（他の IngressRoute は変えない）
        viewer = (Path(__file__).resolve().parents[2] / "k8s" / "audit-viewer.yaml").read_text().replace("${", "{")
        viewer = re.sub(r"\{[A-Z_]+\}", "x", viewer)
        out = list(yaml.safe_load_all(render.add_admin_middleware(viewer)))
        for d in out:
            if d.get("kind") == "IngressRoute" and d["metadata"]["name"] == "audit-viewer":
                for r in d["spec"]["routes"]:
                    self.assertEqual(r["middlewares"][0], {"name": "admin-only"})
        twice = render.add_admin_middleware(render.add_admin_middleware(viewer))
        self.assertEqual(twice.count("admin-only"), render.add_admin_middleware(viewer).count("admin-only"))
        # HTTPS への写しは admin の経路にも効く
        tls = list(yaml.safe_load_all(render.tls_ingressroutes(render.admin_only_manifest(e))))
        self.assertEqual([(d["metadata"]["name"], d["spec"]["entryPoints"]) for d in tls], [("litellm-admin-tls", ["websecure"])])
        pols = list(yaml.safe_load_all((Path(__file__).resolve().parents[2] / "k8s" / "network-policies.yaml").read_text()))
        self.assertEqual({p["metadata"]["name"] for p in pols}, {"postgres-ingress", "pooler-ingress", "valkey-ingress"})
        for p in pols:
            self.assertEqual(p["spec"]["policyTypes"], ["Ingress"])
            self.assertEqual(p["metadata"]["namespace"], "litellm-lab")

    def test_zabbix_api_and_setup_is_idempotent(self):
        """Zabbix（変更設計書 2026-10-31 7 章）: 偽の JSON-RPC サーバーで、トークンの発行、ホストの作成 → 2 回目は何もしない、
        自動登録のアクションの形、HTTP のアイテムとトリガー、Helm の値。"""
        import http.server
        import threading
        from labtool import zabbix
        calls: list[tuple[str, dict]] = []
        state = {"hosts": {}, "groups": {}, "actions": {}, "items": {}, "triggers": {}, "proxies": {}, "tokens": {}, "admin_pw": "zabbix"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method, params = body["method"], body.get("params")
                calls.append((method, params))
                auth = self.headers.get("Authorization", "")
                result = None
                if method == "apiinfo.version":
                    result = "7.0.17"
                elif method == "user.login":
                    if params["password"] != state["admin_pw"]:
                        return self._err(body, "Incorrect user name or password or account is temporarily blocked.")   # 7.0 の文言
                    result = "session-2" if state.get("session_dead") else "session-1"
                elif method == "user.get":
                    result = [{"userid": "1", "username": "Admin"}]
                elif method == "user.update":
                    state["admin_pw"] = params["passwd"]
                    state["session_dead"] = True   # 7.0: パスワードを変えるとセッションが切れる
                    result = {"userids": ["1"]}
                elif state.get("session_dead") and auth == "Bearer session-1":
                    return self._err(body, "Session terminated, re-login, please.")
                elif method == "token.get":
                    result = []
                elif method == "token.create":
                    result = {"tokenids": ["7"]}
                elif method == "token.generate":
                    result = [{"tokenid": "7", "token": "api-token-xyz"}]
                elif method == "user.logout":
                    result = True
                elif auth != "Bearer api-token-xyz":
                    return self._err(body, "Not authorized.")
                elif method == "hostgroup.get":
                    name = params["filter"]["name"]
                    result = [{"groupid": state["groups"][name], "name": name}] if name in state["groups"] else []
                elif method == "hostgroup.create":
                    gid = str(10 + len(state["groups"]))
                    state["groups"][params["name"]] = gid
                    result = {"groupids": [gid]}
                elif method == "template.get":
                    result = [{"templateid": {"Linux by Zabbix agent active": "100", "Kubernetes cluster state by HTTP": "200"}[params["filter"]["host"]]}]
                elif method == "action.get":
                    a = state["actions"].get(params["filter"]["name"])
                    result = [a] if a else []
                elif method == "action.create":
                    state["actions"][params["name"]] = {**params, "actionid": str(len(state["actions"]) + 1), "status": "0"}
                    result = {"actionids": ["1"]}
                elif method == "proxy.get":
                    p = state["proxies"].get(params["filter"]["name"])
                    result = [p] if p else []
                elif method == "proxy.create":
                    state["proxies"][params["name"]] = {"proxyid": "5", "name": params["name"]}
                    result = {"proxyids": ["5"]}
                elif method == "host.get":
                    if "filter" in params:
                        h = state["hosts"].get(params["filter"]["host"])
                        result = [h] if h else []
                    else:
                        result = list(state["hosts"].values())
                elif method == "host.create":
                    hid = str(len(state["hosts"]) + 1)
                    state["hosts"][params["host"]] = {
                        "hostid": hid, "host": params["host"], "status": "0",
                        "parentTemplates": [{"templateid": x["templateid"]} for x in params["templates"]],
                        "hostgroups": [{"groupid": x["groupid"]} for x in params["groups"]],
                        "macros": [{"macro": m["macro"], "value": m["value"]} for m in params["macros"]] + [{"macro": "{$OTHER}", "value": "keep"}],
                        "tags": params.get("tags", []), "monitored_by": str(params.get("monitored_by", 0)), "proxyid": params.get("proxyid", "0")}
                    result = {"hostids": [hid]}
                elif method == "host.update":
                    result = {"hostids": [params["hostid"]]}
                elif method == "item.get":
                    key = params["filter"]["key_"]
                    i = state["items"].get(key)
                    result = [i] if i else []
                elif method == "item.create":
                    state["items"][params["key_"]] = {**params, "itemid": "9", "status": "0"}
                    result = {"itemids": ["9"]}
                elif method == "trigger.get":
                    tr = state["triggers"].get(params["filter"]["description"])
                    result = [tr] if tr else []
                elif method == "trigger.create":
                    state["triggers"][params["description"]] = {**params, "triggerid": "3"}
                    result = {"triggerids": ["3"]}
                else:
                    return self._err(body, f"unknown {method}")
                self._send({"jsonrpc": "2.0", "result": result, "id": body["id"]})

            def _err(self, body, message):
                self._send({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params.", "data": message}, "id": body["id"]})

            def _send(self, doc):
                data = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api_jsonrpc.php"
        try:
            e = VmTargetTest().vm("gcp-vm", LAB_ZABBIX_SERVER="10.60.0.20:10051", LAB_ZABBIX_API_URL=url)
            written = {}
            e.set = lambda k, v: written.__setitem__(k, v) or e.values.__setitem__(k, v)   # .env に書かずに記録
            client = zabbix._bootstrap_token(e, url)
            self.assertEqual(client.token, "api-token-xyz")
            self.assertEqual(written["LAB_ZABBIX_API_TOKEN"], "api-token-xyz")
            self.assertNotEqual(state["admin_pw"], "zabbix")   # 初期パスワードは変わる
            self.assertEqual(written["LAB_ZABBIX_ADMIN_PASSWORD"], state["admin_pw"])
            groups = zabbix.ensure_groups(client)
            self.assertEqual(set(groups), {"vm", "k8s", "service"})
            zabbix.ensure_autoregistration(client, "act", "litellm-lab-node", groups["vm"], "100")
            act = state["actions"]["act"]
            self.assertEqual((act["eventsource"], act["filter"]["conditions"][0]), (2, {"conditiontype": 24, "operator": 2, "value": "litellm-lab-node"}))
            self.assertEqual([o["operationtype"] for o in act["operations"]], [4, 6])
            pid = zabbix.ensure_proxy(client, "p")
            hid = zabbix.ensure_host(client, "k8s", [groups["k8s"]], ["200"], {"{$KUBE.API.URL}": "https://k", "{$KUBE.API.TOKEN}": "t"}, pid,
                                     tags={"lab": "k8s"})
            self.assertEqual(hid, "1")
            self.assertEqual(state["hosts"]["k8s"]["monitored_by"], "1")
            calls.clear()
            zabbix.ensure_groups(client)
            zabbix.ensure_autoregistration(client, "act", "litellm-lab-node", groups["vm"], "100")
            zabbix.ensure_proxy(client, "p")
            zabbix.ensure_host(client, "k8s", [groups["k8s"]], ["200"], {"{$KUBE.API.URL}": "https://k", "{$KUBE.API.TOKEN}": "t"}, pid,
                               tags={"lab": "k8s"})
            self.assertFalse(any(m.endswith((".create", ".update")) for m, _ in calls), [m for m, _ in calls])   # 2 回目は作らない・直さない
            calls.clear()
            zabbix.ensure_host(client, "k8s", [groups["k8s"]], ["200"], {"{$KUBE.API.URL}": "https://k2", "{$KUBE.API.TOKEN}": "t"}, pid,
                               tags={"lab": "k8s"})
            upd = next(p for m, p in calls if m == "host.update")
            self.assertIn({"macro": "{$OTHER}", "value": "keep"}, upd["macros"])   # 管理していないマクロは残す
            self.assertIn({"macro": "{$KUBE.API.URL}", "value": "https://k2"}, upd["macros"])
            zabbix.ensure_http_check(client, hid, "k8s", "liveliness", "https://lab.example.test/health/liveliness")
            item = state["items"]["lab.http[liveliness]"]
            self.assertEqual((item["type"], item["status_codes"], item["url"]), (19, "200", "https://lab.example.test/health/liveliness"))
            self.assertEqual(next(iter(state["triggers"].values()))["expression"], "nodata(/k8s/lab.http[liveliness],180s)=1")
            calls.clear()
            zabbix.ensure_http_check(client, hid, "k8s", "liveliness", "https://lab.example.test/health/liveliness")
            self.assertFalse(any(m.endswith((".create", ".update")) for m, _ in calls))
        finally:
            server.shutdown()
        values = zabbix.chart_values(e, "10.60.0.20:10051")
        self.assertFalse(values["zabbixAgent"]["enabled"])   # VM の agent 2 と 10050 とノード名が重なるので入れない
        self.assertTrue(values["kubeStateMetrics"]["enabled"])
        envs = {x["name"]: x["value"] for x in values["zabbixProxy"]["env"]}
        self.assertEqual((envs["ZBX_SERVER_HOST"], envs["ZBX_HOSTNAME"], envs["ZBX_PROXYMODE"]), ("10.60.0.20:10051", zabbix.PROXY_HOSTNAME, 0))
        self.assertEqual(zabbix.KSM_ENDPOINT, "zabbix-kube-state-metrics")

    def test_infra_helpers(self):
        from labtool import clouds
        infra = self.infra("aws-ec2")
        self.assertEqual(infra.node_ids(), ["id-1", "id-2", "id-3"])
        self.assertEqual(infra.node("id-2").private_ip, "10.60.1.10")
        self.assertEqual((infra.bucket, infra.proxy), ("lab-bucket", None))
        with self.assertRaises(Exception):
            infra.node("id-9")
        plan = clouds.network_plan("10.60.0.0/16", True)
        self.assertEqual((plan["vpc"], plan["proxy_ip"]), ("10.60.0.0/16", "10.60.0.20"))   # 中継の VM は先頭のサブネットの .20

    def test_egress_env_and_add_egress(self):
        """出口プロキシ（変更設計書 7.2 節）: 中継があれば全コンテナに環境変数とマウント、ObjectStore に sidecar の env と endpointCA。無ければ変えない。"""
        import os
        import tempfile
        from labtool.core import HELM_EXTRA, helm
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write("-----BEGIN CERTIFICATE-----\nQ0E=\n-----END CERTIFICATE-----\n")
            ca = f.name
        e = VmTargetTest().vm("gcp-vm", LAB_EGRESS_PROXY="http://10.60.0.20:3128", LAB_EGRESS_CA_FILE=ca)
        envs = {x["name"]: x["value"] for x in render.egress_env(e)}
        self.assertEqual((envs["HTTPS_PROXY"], envs["http_proxy"]), ("http://10.60.0.20:3128", "http://10.60.0.20:3128"))
        self.assertIn("fake-llm", envs["NO_PROXY"])
        self.assertIn("10.43.0.0/16", envs["no_proxy"])
        self.assertEqual((envs["SSL_CERT_FILE"], envs["AWS_CA_BUNDLE"], envs["REQUESTS_CA_BUNDLE"]), (render.EGRESS_CA_PATH,) * 3)
        self.assertEqual(render.egress_env(VmTargetTest().vm("gcp-vm")), [])
        docs = [{"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"name": "a", "env": [{"name": "X", "value": "1"}]},
                                                                                     {"name": "b"}]}}}},
                {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}}}},
                {"kind": "ObjectStore", "spec": {"configuration": {"destinationPath": "s3://b/p"},
                                                 "instanceSidecarConfiguration": {"env": [{"name": "AWS_DEFAULT_REGION", "value": "r"}]}}},
                {"kind": "Service", "spec": {}}]
        out = render.add_egress(docs, e)
        pod = out[0]["spec"]["template"]["spec"]
        for c in pod["containers"]:
            self.assertIn("HTTPS_PROXY", {x["name"] for x in c["env"]})
            self.assertEqual(c["volumeMounts"], [render.egress_mount()])
        self.assertEqual({x["name"] for x in pod["containers"][0]["env"]} >= {"X", "HTTPS_PROXY"}, True)
        self.assertEqual(pod["volumes"], [render.egress_volume()])
        self.assertIn("HTTPS_PROXY", {x["name"] for x in out[1]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]})
        store = out[2]["spec"]
        self.assertEqual(store["configuration"]["endpointCA"], {"name": render.EGRESS_CA_NAME, "key": "ca.crt"})
        side = {x["name"] for x in store["instanceSidecarConfiguration"]["env"]}
        self.assertIn("HTTPS_PROXY", side)
        self.assertNotIn("AWS_CA_BUNDLE", side)   # CA は endpointCA で渡す
        self.assertEqual(out[3], {"kind": "Service", "spec": {}})
        # 中継が無ければそのまま
        plain = VmTargetTest().vm("gcp-vm")
        self.assertEqual(render.add_egress([{"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"name": "a"}]}}}}], plain),
                         [{"kind": "Deployment", "spec": {"template": {"spec": {"containers": [{"name": "a"}]}}}}])
        self.assertEqual(render.cert_manager_egress_values(plain), {})
        # DNS-01 の伝搬確認はクラウドの再帰リゾルバに聞く（Pod から外の 53 へは出られない）
        self.assertEqual(render.cert_manager_egress_values(plain, ["169.254.169.254"]),
                         {"dns01RecursiveNameservers": "169.254.169.254:53", "dns01RecursiveNameserversOnly": True})
        cm = render.cert_manager_egress_values(e)
        self.assertEqual((cm["https_proxy"], cm["extraEnv"][0]["name"]), ("http://10.60.0.20:3128", "SSL_CERT_FILE"))
        ca_docs = list(yaml.safe_load_all(render.egress_ca_manifests(e, ["litellm-lab", "cert-manager"])))
        self.assertEqual([(d["kind"], d["metadata"]["namespace"]) for d in ca_docs],
                         [("ConfigMap", "litellm-lab"), ("Secret", "litellm-lab"), ("ConfigMap", "cert-manager"), ("Secret", "cert-manager")])
        self.assertEqual(render.egress_ca_manifests(plain, ["litellm-lab"]), "")
        # LiteLLM の Helm の値: 中継があれば環境変数とマウント、aiohttp を切る
        o = render.helm_overrides(e, "r", "t", "h")
        self.assertEqual((o["envVars"]["HTTPS_PROXY"], o["envVars"]["DISABLE_AIOHTTP_TRANSPORT"]), ("http://10.60.0.20:3128", "True"))
        self.assertIn(render.EGRESS_CA_NAME, [v["name"] for v in o["volumes"]])
        # 操作者の Mac も中継を通す: 環境変数と helm の --ca-file（チャートを取るサブコマンドだけ）
        saved = dict(os.environ)
        import labtool.core as core
        core.MAC_PROXY_CHECK = False
        try:
            m = VmTargetTest().vm("gcp-vm", LAB_EGRESS_PROXY_MAC="http://203.0.113.20:3128", LAB_EGRESS_CA_FILE=ca)
            m.apply_mac_proxy()
            self.assertEqual((os.environ["HTTPS_PROXY"], os.environ["NO_PROXY"]), ("http://203.0.113.20:3128", "127.0.0.1,localhost"))
            # 中継の CA だけでなく公開 CA も含む束（中継を通らない転送口の Let's Encrypt の証明書も検証できる）
            bundle = os.environ["REQUESTS_CA_BUNDLE"]
            self.assertEqual(os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"], bundle)
            self.assertTrue(bundle.endswith("mac-ca-bundle.pem"))
            text = Path(bundle).read_text()
            self.assertIn("Q0E=", text)
            self.assertGreater(text.count("BEGIN CERTIFICATE"), 10)
            self.assertEqual(helm("upgrade", "--install", "x", "y")[-2:], ["--ca-file", bundle])
            self.assertNotIn("--ca-file", helm("-n", "ns", "get", "values", "x"))
        finally:
            os.environ.clear()
            os.environ.update(saved)
            HELM_EXTRA.clear()
            core.MAC_PROXY_CHECK = True
        os.unlink(ca)