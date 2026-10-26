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


class Ec2TargetTest(unittest.TestCase):
    """LAB_TARGET=aws-ec2（EC2 3 台の k3s と S3）のときの描画と CloudFormation のテンプレート
    （変更仕様書 .history/2026-10-25-EC2三台構成/ 2.4 節、変更設計書 3 章）。local と aws の振る舞いは上のテストで変わらないこと。"""

    OUT = {"EipAllocationId": "eipalloc-1", "EipPublicIp": "203.0.113.10", "InstanceIds": "i-1,i-2,i-3",
           "ImagesBucketName": "litellm-lab-111111111111-images",
           "LitellmWriterAccessKeyId": "AKIAWRITER", "LitellmWriterSecretAccessKey": "sw",
           "AuditCompactorAccessKeyId": "AKIACOMP", "AuditCompactorSecretAccessKey": "sc",
           "AuditViewerAccessKeyId": "AKIAVIEW", "AuditViewerSecretAccessKey": "sv",
           "PgBackupAccessKeyId": "AKIAPG", "PgBackupSecretAccessKey": "sp",
           "RegistryAccessKeyId": "AKIAREG", "RegistrySecretAccessKey": "sr",
           "LitellmBedrockAccessKeyId": "AKIABED", "LitellmBedrockSecretAccessKey": "sb"}

    def ec2(self, **extra):
        e = env(LAB_TARGET="aws-ec2", LAB_AWS_HOSTNAME="lab.example.test", LAB_ACME_EMAIL="ops@example.test",
                TYPESAFE_API_KEY="t", **extra)
        e.values["LAB_AWS_BUCKET_PREFIX"] = render.bucket_prefix(e, "111111111111")
        return e

    def test_target_flags_and_urls(self):
        e = self.ec2()
        self.assertTrue(e.is_aws_ec2 and e.on_aws and not e.is_aws)
        self.assertFalse(env().on_aws)
        self.assertEqual(e.base_url, "https://lab.example.test")
        self.assertEqual(e.ec2_prefix, "litellm-lab-ec2")
        self.assertEqual((e.ec2_instance_type, e.ec2_vpc_cidr, e.ec2_local_port, e.ec2_keep_eip),
                         ("t4g.large", "10.60.0.0/16", 16443, False))
        self.assertEqual(render.bucket_name(e, "audit-raw"), "litellm-lab-111111111111-audit-raw")

    def test_models_use_bedrock_with_static_keys_and_no_chatgpt(self):
        cfg = render.litellm_config(self.ec2(), has_chatgpt=False)
        models = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
        self.assertTrue(models["claude-sonnet-4-5"]["model"].startswith("bedrock/jp.anthropic.claude-sonnet-4-5"))
        self.assertEqual(models["claude-sonnet-4-5"]["aws_access_key_id"], "os.environ/BEDROCK_ACCESS_KEY_ID")
        self.assertEqual(models["review-judge"]["aws_secret_access_key"], "os.environ/BEDROCK_SECRET_ACCESS_KEY")
        self.assertFalse(cfg["guardrails"][0]["litellm_params"]["use_jev"])
        # EKS 版は Pod Identity（資格情報チェーン）なので静的キーの指定が無い
        aws_cfg = render.litellm_config(env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="x.test"), has_chatgpt=False)
        self.assertNotIn("aws_access_key_id", {m["model_name"]: m["litellm_params"] for m in aws_cfg["model_list"]}["review-judge"])
        secrets = render.secrets_for_litellm(self.ec2(BEDROCK_ACCESS_KEY_ID="AKIABED", BEDROCK_SECRET_ACCESS_KEY="sb"))
        self.assertEqual((secrets["BEDROCK_ACCESS_KEY_ID"], secrets["BEDROCK_SECRET_ACCESS_KEY"]), ("AKIABED", "sb"))
        self.assertNotIn("BEDROCK_ACCESS_KEY_ID", render.secrets_for_litellm(env()))
        contracts = {c["id"] for c in render.initial_contracts(self.ec2())["contracts"]}
        self.assertEqual(contracts, {"bedrock"})   # chatgpt/ を置かない

    def test_helm_overrides_keep_local_shape_but_use_aws_s3(self):
        o = render.helm_overrides(self.ec2(), "lab-registry:5000/litellm-lab", "t", "h")
        ev = o["envVars"]
        self.assertIn("REDIS_SENTINEL_NODES", ev)                      # Valkey は Mac と同じ Sentinel
        self.assertEqual(o["db"]["endpoint"], "litellm-pg-pooler-rw")  # Pooler も Mac と同じ
        self.assertEqual(ev["AUDIT_S3_ENDPOINT"], "")
        self.assertEqual(ev["AUDIT_S3_BUCKET"], "litellm-lab-111111111111-audit-raw")
        self.assertEqual(ev["AUDIT_S3_REGION"], "ap-northeast-1")
        self.assertNotIn("AUDIT_S3_CREDENTIAL_CHAIN", ev)              # 静的キー（Secret litellm-secrets）
        self.assertEqual([v["name"] for v in o["volumes"]], ["audit-spool"])   # chatgpt/ の hostPath は無い

    def test_pg_backup_values(self):
        local = render.pg_backup_values(env())
        self.assertEqual(local["S3_BUCKET_PREFIX"], "")
        self.assertEqual(local["S3_ENDPOINT_LINE"], "endpointURL: http://rustfs:9000")
        e = render.pg_backup_values(self.ec2())
        self.assertEqual((e["S3_BUCKET_PREFIX"], e["S3_ENDPOINT_LINE"], e["S3_REGION"]),
                         ("litellm-lab-111111111111-", "", "ap-northeast-1"))
        text = render_template(Path(__file__).resolve().parents[2] / "k8s" / "postgres-cnpg.yaml", e)
        store = next(d for d in yaml.safe_load_all(text) if d["kind"] == "ObjectStore")
        self.assertEqual(store["spec"]["configuration"]["destinationPath"], "s3://litellm-lab-111111111111-pg-backup/litellm-pg")
        self.assertNotIn("endpointURL", store["spec"]["configuration"])
        local_text = render_template(Path(__file__).resolve().parents[2] / "k8s" / "postgres-cnpg.yaml", local)
        self.assertIn("endpointURL: http://rustfs:9000", local_text)

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
        issuer = yaml.safe_load(render.ec2_issuer_manifest(self.ec2()))
        self.assertEqual(issuer["spec"]["acme"]["server"], render.ACME_PRODUCTION)
        self.assertEqual(issuer["spec"]["acme"]["solvers"][0]["dns01"]["cloudflare"]["apiTokenSecretRef"]["name"],
                         "cloudflare-api-token")
        self.assertEqual(yaml.safe_load(render.ec2_issuer_manifest(self.ec2(LAB_ACME_STAGING="true")))["spec"]["acme"]["server"],
                         render.ACME_STAGING)
        with self.assertRaises(ValueError):
            render.ec2_issuer_manifest(env(LAB_TARGET="aws-ec2", LAB_AWS_HOSTNAME="x.test"))
        cert = yaml.safe_load(render.ec2_certificate_manifest(self.ec2()))
        self.assertEqual((cert["spec"]["dnsNames"], cert["spec"]["secretName"]), (["lab.example.test"], "litellm-tls"))
        docs = list(yaml.safe_load_all(render.ec2_registry_manifest(self.ec2(), "litellm-lab-111111111111-images", 30500)))
        svc = next(d for d in docs if d["kind"] == "Service")
        self.assertEqual((svc["spec"]["type"], svc["spec"]["ports"][0]["nodePort"]), ("NodePort", 30500))
        dep = next(d for d in docs if d["kind"] == "Deployment")
        envs = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual((envs["REGISTRY_STORAGE"], envs["REGISTRY_STORAGE_S3_BUCKET"]), ("s3", "litellm-lab-111111111111-images"))
        self.assertEqual(dep["spec"]["replicas"], 2)
        secret = render.ec2_registry_secret(self.ec2(S3_REGISTRY_KEY="AKIAREG", S3_REGISTRY_SECRET="sr",
                                                     LAB_REGISTRY_HTTP_SECRET="h"))
        self.assertEqual(secret, {"REGISTRY_STORAGE_S3_ACCESSKEY": "AKIAREG", "REGISTRY_STORAGE_S3_SECRETKEY": "sr",
                                  "REGISTRY_HTTP_SECRET": "h"})

    def test_load_credentials_from_outputs(self):
        from labtool import ec2
        e = self.ec2()
        ec2.load_credentials(e, self.OUT)
        self.assertEqual((e.get("S3_LITELLM_WRITER_KEY"), e.get("S3_LITELLM_WRITER_SECRET")), ("AKIAWRITER", "sw"))
        self.assertEqual(e.get("S3_REGISTRY_KEY"), "AKIAREG")
        self.assertEqual(e.get("BEDROCK_SECRET_ACCESS_KEY"), "sb")
        self.assertEqual(ec2.instance_ids(self.OUT), ["i-1", "i-2", "i-3"])
        self.assertEqual(ec2.token_parameter(e), "/litellm-lab-ec2/k3s-token")

    def test_failover_scenarios(self):
        from labtool import failover
        self.assertEqual(failover.scenarios_for(self.ec2()), [*failover.SCENARIOS, "eip-node"])
        self.assertEqual(failover.scenarios_for(env()), failover.SCENARIOS)
        self.assertEqual(failover.scenarios_for(env(LAB_TARGET="aws", LAB_AWS_HOSTNAME="x")), failover.SCENARIOS_AWS)
        self.assertEqual(failover.LIMITS["eip-node"], 30)

    def test_k3s_version_rules(self):
        """k3s の版上げ（./lab upgrade-k8s）: 版の形、マイナー版を 1 つずつ、下げない。"""
        from labtool import ec2
        from labtool.core import LabError
        self.assertEqual(ec2.parse_k3s_version("v1.35.5+k3s1"), (1, 35, 5))
        self.assertEqual(ec2.normalize_k3s_version("v1.36.1-k3s1"), "v1.36.1+k3s1")
        ec2.check_upgrade_step("v1.35.5+k3s1", "v1.36.1+k3s1")    # 次のマイナー版
        ec2.check_upgrade_step("v1.35.5+k3s1", "v1.35.7+k3s1")    # 同じマイナー版のパッチ
        for bad in ("v1.37.0+k3s1", "v1.35.4+k3s1", "v1.34.9+k3s1", "v2.0.0+k3s1"):
            with self.assertRaises(LabError):
                ec2.check_upgrade_step("v1.35.5+k3s1", bad)
        with self.assertRaises(LabError):
            ec2.parse_k3s_version("1.36.1")
        self.assertEqual(ec2.version_parameter(self.ec2()), "/litellm-lab-ec2/k3s-version")

    def test_k3s_upgrade_order_puts_eip_holder_last(self):
        from labtool import ec2
        ids = ["i-1", "i-2", "i-3"]
        self.assertEqual(ec2.upgrade_order(ids, "i-2"), ["i-1", "i-3", "i-2"])
        self.assertEqual(ec2.upgrade_order(ids, "i-2", forward_target="i-1"), ["i-3", "i-1", "i-2"])
        self.assertEqual(ec2.upgrade_order(ids, "i-2", forward_target="i-2"), ["i-1", "i-3", "i-2"])
        self.assertEqual(ec2.upgrade_order(ids, None), ids)

    def test_litellm_pdb_manifest(self):
        pdb = json.loads(render.litellm_pdb_manifest())
        self.assertEqual(pdb["kind"], "PodDisruptionBudget")
        self.assertEqual(pdb["spec"]["minAvailable"], 1)
        self.assertEqual(pdb["spec"]["selector"]["matchLabels"], {"app.kubernetes.io/name": "litellm"})


class CfnTemplateTest(unittest.TestCase):
    """CloudFormation のテンプレート（labtool/cfn.py）。"""

    def ec2(self, **extra):
        e = env(LAB_TARGET="aws-ec2", LAB_AWS_HOSTNAME="lab.example.test", LAB_ACME_EMAIL="ops@example.test", **extra)
        e.values["LAB_AWS_BUCKET_PREFIX"] = render.bucket_prefix(e, "111111111111")
        return e

    def test_network_plan_and_version(self):
        from labtool import cfn
        plan = cfn.network_plan("10.60.0.0/16")
        self.assertEqual(plan["subnets"], ["10.60.0.0/24", "10.60.1.0/24", "10.60.2.0/24"])
        self.assertEqual(plan["nodes"][0], {"index": 1, "subnet": "10.60.0.0/24", "private_ip": "10.60.0.10",
                                            "secondary_ip": "10.60.0.11"})
        self.assertEqual(cfn.k3s_install_version("rancher/k3s:v1.35.5-k3s1"), "v1.35.5+k3s1")
        self.assertEqual(cfn.camel("audit-raw"), "AuditRaw")
        self.assertEqual(cfn.output_key("litellm-bedrock", "AccessKeyId"), "LitellmBedrockAccessKeyId")

    def test_storage_template(self):
        from labtool import cfn
        doc = cfn.storage_template(self.ec2(), "111111111111")
        res = doc["Resources"]
        raw = res["AuditRawBucket"]["Properties"]
        self.assertEqual(raw["BucketName"], "litellm-lab-111111111111-audit-raw")
        self.assertTrue(raw["ObjectLockEnabled"])
        self.assertEqual(raw["ObjectLockConfiguration"]["Rule"]["DefaultRetention"], {"Mode": "GOVERNANCE", "Days": 90})
        self.assertEqual(raw["LifecycleConfiguration"]["Rules"][0]["ExpirationInDays"], 91)
        self.assertNotIn("Prefix", raw["LifecycleConfiguration"]["Rules"][0])
        derived = res["AuditDerivedBucket"]["Properties"]
        self.assertNotIn("ObjectLockEnabled", derived)
        self.assertEqual(next(r for r in derived["LifecycleConfiguration"]["Rules"] if r["Id"] == "synth")["Prefix"], "synth/")
        self.assertIn("PgBackupBucket", res)   # aws-ec2 は CloudNativePG のバックアップ先が要る（EKS 版は作らない）
        self.assertIn("ImagesBucket", res)
        self.assertTrue(all(res[k]["DeletionPolicy"] == "Delete" for k in res if k.endswith("Bucket")))
        # IAM ユーザー: S3 の 4 利用者 ＋ registry ＋ litellm-bedrock。権限は接頭辞付きの ARN で、監査ログの削除を明示的に拒否
        users = {k for k in res if res[k]["Type"] == "AWS::IAM::User"}
        self.assertEqual(users, {"LitellmWriterUser", "AuditCompactorUser", "AuditViewerUser", "PgBackupUser",
                                 "RegistryUser", "LitellmBedrockUser"})
        writer = res["LitellmWriterUser"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertEqual(writer[0]["Resource"], ["arn:aws:s3:::litellm-lab-111111111111-audit-raw/raw/*"])
        self.assertEqual(writer[-1]["Effect"], "Deny")
        self.assertIn("s3:BypassGovernanceRetention", writer[-1]["Action"])
        bedrock = res["LitellmBedrockUser"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
        self.assertEqual(bedrock["Action"], ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"])
        out = doc["Outputs"]
        self.assertEqual(out["LitellmWriterSecretAccessKey"]["Value"], {"Fn::GetAtt": ["LitellmWriterKey", "SecretAccessKey"]})
        self.assertEqual(out["ImagesBucketName"]["Value"], {"Ref": "ImagesBucket"})
        self.assertEqual(res["LitellmWriterUser"]["Properties"]["UserName"], "litellm-lab-ec2-litellm-writer")

    def test_network_and_nodes_templates(self):
        from labtool import cfn
        net = cfn.network_template(self.ec2())
        res = net["Resources"]
        self.assertEqual(res["Vpc"]["Properties"]["CidrBlock"], "10.60.0.0/16")
        self.assertEqual(res["Subnet3"]["Properties"]["AvailabilityZone"], {"Fn::Select": [2, {"Fn::GetAZs": ""}]})
        self.assertEqual(res["S3Endpoint"]["Properties"]["VpcEndpointType"], "Gateway")
        ports = {(r["FromPort"], r["ToPort"]) for r in res["NodeSecurityGroup"]["Properties"]["SecurityGroupIngress"]}
        self.assertEqual(ports, {(80, 80), (443, 443)})           # 外から開けるのは 80 と 443 だけ
        self.assertEqual(res["NodeToNode"]["Properties"]["IpProtocol"], "-1")
        script = "#!/bin/bash\nR=@@REGION@@ I=@@NODE_INDEX@@ S=@@SELF_IP@@ F=@@FIRST_IP@@ P=\"@@PEER_IPS@@\" Y=@@SECONDARY_IP@@ " \
                 "V=@@VERSION_PARAM@@ T=@@TOKEN_PARAM@@ E=@@EIP_ALLOCATION_ID@@ Q=@@PRIORITY@@ H=@@REGISTRY_HOST@@ N=@@REGISTRY_NODEPORT@@\n"
        nodes = cfn.nodes_template(self.ec2(), "/litellm-lab-ec2/k3s-token", "eipalloc-1", script,
                                   version_param="/litellm-lab-ec2/k3s-version")
        res = nodes["Resources"]
        self.assertEqual({k for k in res if res[k]["Type"] == "AWS::EC2::Instance"}, {"Node1", "Node2", "Node3"})
        n2 = res["Node2"]["Properties"]
        self.assertEqual(n2["NetworkInterfaces"][0]["PrivateIpAddresses"],
                         [{"PrivateIpAddress": "10.60.1.10", "Primary": True}, {"PrivateIpAddress": "10.60.1.11", "Primary": False}])
        self.assertEqual(n2["NetworkInterfaces"][0]["SubnetId"], {"Ref": "Subnet2Id"})
        self.assertEqual(n2["MetadataOptions"]["HttpTokens"], "required")
        self.assertEqual(n2["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 40)
        user_data = n2["UserData"]["Fn::Base64"]
        self.assertIn("I=2 S=10.60.1.10 F=10.60.0.10 P=\"10.60.0.10 10.60.2.10\" Y=10.60.1.11", user_data)
        self.assertIn("V=/litellm-lab-ec2/k3s-version T=/litellm-lab-ec2/k3s-token E=eipalloc-1 Q=150 H=lab-registry:5000 N=30500",
                      user_data)
        self.assertNotIn("@@", user_data)
        self.assertNotIn("v1.35.5", user_data)   # 版は user data に書かない（上げても 3 台が再起動しない）
        # AMI は固定の ID で受ける（「最新」の SSM パラメータを参照すると、新しい AMI で 3 台が同時に置き換えられる）
        self.assertEqual(nodes["Parameters"]["AmiId"], {"Type": "AWS::EC2::Image::Id"})
        role = res["NodeRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertIn("ec2:AssociateAddress", role[0]["Action"])
        self.assertEqual(role[1]["Action"], ["ssm:GetParameter"])
        self.assertEqual(len(role[1]["Resource"]), 2)   # 参加トークンと版の 2 つのパラメータだけ
        self.assertIn("k3s-version", role[1]["Resource"][1]["Fn::Sub"])
        with self.assertRaises(ValueError):
            cfn.render_node_init(self.ec2(), cfn.network_plan("10.60.0.0/16")["nodes"][0], cfn.network_plan("10.60.0.0/16"),
                                 "/p", "eipalloc-1", "@@UNKNOWN@@")

    def test_real_node_init_script_has_no_leftover_placeholders(self):
        from labtool import cfn
        plan = cfn.network_plan("10.60.0.0/16")
        text = cfn.render_node_init(self.ec2(), plan["nodes"][2], plan, "/litellm-lab-ec2/k3s-token", "eipalloc-1",
                                    version_param="/litellm-lab-ec2/k3s-version")
        self.assertIn('PRIORITY="100"', text)
        self.assertIn('VERSION_PARAM="/litellm-lab-ec2/k3s-version"', text)
        # 引数は config.yaml に書き、インストールスクリプトには server だけ渡す（版上げの再実行で引数が消えない）
        self.assertIn("/etc/rancher/k3s/config.yaml", text)
        self.assertIn("cluster-init: true", text)
        self.assertIn("sh -s - server\n", text)
        self.assertNotIn("--cluster-init", text)
        self.assertIn("nopreempt", text)
        self.assertIn("associate-address", text)
        eip = cfn.eip_template(self.ec2())
        self.assertEqual(eip["Resources"]["EntryIp"]["Type"], "AWS::EC2::EIP")
