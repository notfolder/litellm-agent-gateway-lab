"""ECS 構成の 1 ファイル（ecs/ecs-lab.sh）が、labtool と同じ中身を作ることを確かめる（変更設計書 .history/2026-11-21-ECS構成/ 12 章）。

ecs-lab.sh は bash だけで動くように labtool/render.py を読まずに書き写しているので、同じ .env から作った物を突き合わせる。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from labtool import render
from labtool.core import ROOT, Env

SCRIPT = ROOT / "ecs" / "ecs-lab.sh"

BASE_ENV = {
    "LAB_TARGET": "aws-ecs",
    "LAB_ECS_HOSTNAME": "litellm-ecs.example.org",
    "LAB_CLOUDFLARE_API_TOKEN": "x",
    "LITELLM_MASTER_KEY": "sk-test",
    "LAB_JUDGE_ANTHROPIC_API_KEY": "sk-ant-test",
}


def run_script(command: str, values: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        proc = subprocess.run(["bash", str(SCRIPT), command], capture_output=True, text=True,
                              env={**os.environ, "ECS_LAB_ENV": str(env_file)}, check=True)
        return proc.stdout


class CfnLoader(yaml.SafeLoader):
    """CloudFormation の短縮形のタグ（!Ref など）を {"Ref": ...} の形で読む。"""


def _cfn_tag(loader: yaml.Loader, suffix: str, node: yaml.Node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


class EcsConfigTest(unittest.TestCase):
    """LiteLLM の設定: ecs-lab.sh render-config と render.litellm_config が同じ（chatgpt/ は ECS 構成では置かない）。"""

    def check(self, extra: dict[str, str]) -> None:
        values = {**BASE_ENV, **extra}
        got = yaml.safe_load(run_script("render-config", values))
        want = render.litellm_config(Env(values), has_chatgpt=False)
        self.assertEqual(got, want)

    def test_default(self):
        self.check({})

    def test_with_google_and_options(self):
        self.check({"GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "s",
                    "LAB_CONTEXT_REVIEW_CLAUDE_CODE": "true", "LAB_ISOLATED_DESTINATIONS": "private, github.com",
                    "LAB_DB_POOLER": "direct", "LAB_VM_JUDGE_MODEL": "claude-haiku-5-5"})

    def test_without_isolated_review(self):
        self.check({"LAB_ISOLATED_REVIEW": "false"})

    def test_without_content_review(self):
        self.check({"LAB_ISOLATED_CONTENT": "false"})


class EcsTemplateTest(unittest.TestCase):
    """テンプレート: バケットの定義と、役割ごとの S3 の許可が render と同じ。"""

    @classmethod
    def setUpClass(cls):
        cls.templates = {}
        for infra in ("managed", "provided"):
            extra = {"LAB_ECS_INFRA": infra}
            if infra == "provided":
                extra |= {"LAB_ECS_VPC_ID": "vpc-0123456789abcdef0", "LAB_ECS_BASTION_CIDR": "10.0.0.10/32",
                          "LAB_ECS_SUBNET_IDS": "subnet-a,subnet-b,subnet-c"}
            cls.templates[infra] = yaml.load(run_script("render-template", {**BASE_ENV, **extra}), Loader=CfnLoader)

    def resources(self, infra: str = "managed") -> dict:
        return self.templates[infra]["Resources"]

    def test_buckets_match_render(self):
        want = render.s3_buckets(Env(BASE_ENV))
        res = self.resources()
        for logical, rid, days_param, expire_param in ((render.AUDIT_RAW, "RawBucket", "RetentionDays", "RetentionExpireDays"),
                                                      (render.AUDIT_ACCESS, "AccessBucket", "AccessRetentionDays", "AccessExpireDays")):
            props = res[rid]["Properties"]
            self.assertTrue(props["ObjectLockEnabled"])
            self.assertEqual(props["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]["Days"], {"Ref": days_param})
            self.assertIsNotNone(want[logical]["lock"])
            rule = props["LifecycleConfiguration"]["Rules"][0]
            self.assertEqual(rule["ExpirationInDays"], {"Ref": expire_param})
            self.assertEqual(want[logical]["lifecycle"][0]["Expiration"]["Days"], 91)   # 既定 90 日 + 1
        derived = res["DerivedBucket"]["Properties"]
        self.assertNotIn("ObjectLockEnabled", derived)
        got_rules = {r["Id"]: r for r in derived["LifecycleConfiguration"]["Rules"]}
        for r in want[render.AUDIT_DERIVED]["lifecycle"]:
            g = got_rules[r["ID"]]
            self.assertEqual(g["Prefix"], r["Filter"]["Prefix"])
            exp = g["ExpirationInDays"]
            self.assertEqual(91 if exp == {"Ref": "RetentionExpireDays"} else exp, r["Expiration"]["Days"])
            self.assertEqual(g["NoncurrentVersionExpiration"]["NoncurrentDays"], r["NoncurrentVersionExpiration"]["NoncurrentDays"])

    def _allowed(self, role: str) -> set[tuple[str, str]]:
        """役割の Allow のうち S3 のもの: (操作, 論理名のパス)。バケット名の接頭辞は外す。"""
        out = set()
        for stmt in self.resources()[role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if stmt["Effect"] != "Allow" or not all(a.startswith("s3:") for a in actions):
                continue
            resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
            for r in resources:
                text = r["Fn::Sub"]
                m = re.fullmatch(r"arn:aws:s3:::\$\{Prefix\}-\$\{AWS::AccountId\}-(.+)", text)
                if not m:
                    continue   # 設定のバケット（LiteLLM の設定）は render に無い
                for a in actions:
                    out.add((a, m.group(1)))
        return out

    def test_role_policies_match_render(self):
        policies = render.s3_policies()
        for user, role in (("litellm-writer", "LitellmRole"), ("audit-viewer", "ViewerRole"), ("audit-compactor", "CompactorRole")):
            want = set()
            for stmt in policies[user]["Statement"]:
                for a in stmt["Action"]:
                    for r in stmt["Resource"]:
                        want.add((a, r.removeprefix("arn:aws:s3:::")))
            self.assertEqual(self._allowed(role), want, role)

    def test_every_task_role_denies_lock_bypass(self):
        for role in ("LitellmRole", "ViewerRole", "CompactorRole", "BasicRole"):
            denies = [s for s in self.resources()[role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
                      if s["Effect"] == "Deny"]
            self.assertEqual(len(denies), 1, role)
            self.assertIn("s3:BypassGovernanceRetention", denies[0]["Action"])
            self.assertIn("s3:DeleteObjectVersion", denies[0]["Action"])

    def test_admin_paths_match_render(self):
        rule = self.resources()["RuleAdminAllow"]["Properties"]["Conditions"][0]["PathPatternConfig"]["Values"]
        self.assertEqual(rule, [p + "*" for p in render.ADMIN_PATH_PREFIXES])

    def test_private_subnets_have_no_internet_route(self):
        res = self.resources()
        routes = [r for r in res.values() if r["Type"] == "AWS::EC2::Route"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["Properties"]["RouteTableId"], {"Ref": "RelayRoutes"})
        self.assertNotIn("Fn::NatGateway", str(res))
        self.assertFalse(any(r["Type"] == "AWS::EC2::NatGateway" for r in res.values()))

    def test_provided_creates_no_network(self):
        res = self.resources("provided")
        kinds = {r["Type"] for r in res.values()}
        for kind in ("AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::VPCEndpoint", "AWS::EC2::InternetGateway", "AWS::EC2::EIP"):
            self.assertNotIn(kind, kinds)
        self.assertIn("AWS::AutoScaling::AutoScalingGroup", kinds)   # 本番の監視の台は落ちたら作り直す

    def test_valkey_pinned_to_one_az_each(self):
        for i in range(3):
            for kind in ("Valkey", "Sentinel"):
                subnets = self.resources()[f"{kind}{i}Service"]["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]
                self.assertEqual(subnets, [{"Ref": f"Private{i}"}])


class EcsScriptsTest(unittest.TestCase):
    """Valkey と Sentinel の設定の行が k8s/valkey/start-*.sh と同じ（名前の付け方だけが違う）。"""

    @staticmethod
    def config_lines(text: str) -> list[str]:
        out = []
        for line in text.splitlines():
            line = line.strip()
            m = re.fullmatch(r'echo "(.+)"', line)
            if m and not m.group(1).startswith("start as"):
                out.append(m.group(1))
            elif re.match(r"^(port|sentinel|requirepass) ", line):
                out.append(line)
        return out

    def script(self, func: str) -> str:
        text = SCRIPT.read_text()
        start = text.index(f"\n{func}() {{\n") + 1
        body = text[start:text.index("\n}\n", start) + 3]
        return subprocess.run(["bash", "-c", body + func], capture_output=True, text=True, check=True).stdout

    def test_valkey_config_lines(self):
        k8s = self.config_lines((ROOT / "k8s/valkey/start-valkey.sh").read_text())
        self.assertEqual(self.config_lines(self.script("valkey_script")), k8s)

    def test_sentinel_config_lines(self):
        k8s = self.config_lines((ROOT / "k8s/valkey/start-sentinel.sh").read_text())
        self.assertEqual(self.config_lines(self.script("sentinel_script")), k8s)


if __name__ == "__main__":
    unittest.main()


class EcsZabbixTest(unittest.TestCase):
    """Zabbix の取り込みの定義: LiteLLM の指標と警報の線が labtool/zabbix_monitors.py と同じ。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(run_script("render-zabbix", BASE_ENV))["zabbix_export"]["templates"][0]

    def test_litellm_counters_match(self):
        from labtool import zabbix_monitors as zm
        rule = next(r for r in self.doc["discovery_rules"] if r["key"] == "lab.ecs.tasks.discovery")
        got = {p["key"]: p["preprocessing"][0]["parameters"][0] for p in rule["item_prototypes"] if p["type"] == "DEPENDENT"}
        want = {f"lab.litellm.{key}.rate[{{#TASK}}]": metric for key, _label, metric, _units in zm.LITELLM_COUNTERS}
        self.assertEqual(got, want)
        totals = {i["key"] for i in self.doc["items"] if i.get("type") == "CALCULATED"}
        for key, *_ in zm.LITELLM_COUNTERS:
            self.assertIn(f"lab.litellm.{key}.rate", totals)

    def test_fail_threshold_matches(self):
        from labtool import zabbix_monitors as zm
        item = next(i for i in self.doc["items"] if i["key"] == "lab.litellm.fail.pct")
        self.assertTrue(item["triggers"][0]["expression"].endswith(f",5m)>{zm.LITELLM_FAIL_PCT}"))

    def test_uuids_are_v4_and_unique(self):
        uuids = re.findall(r"uuid: ([0-9a-f]+)", run_script("render-zabbix", BASE_ENV))
        self.assertEqual(len(uuids), len(set(uuids)))
        for u in uuids:
            self.assertRegex(u, r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")


class EcsBillingTest(unittest.TestCase):
    """請求の初期値: render.initial_contracts / initial_fx_rates（VM 構成と同じ契約）と同じ JSON。"""

    def test_initial_billing_matches(self):
        import json
        contracts, fx = run_script("render-billing", BASE_ENV).splitlines()[:2]
        env = Env(BASE_ENV)
        self.assertEqual(json.loads(contracts), render.initial_contracts(env))
        self.assertEqual(json.loads(fx), render.initial_fx_rates(env))
