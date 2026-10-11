"""精算の計算、請求の単位、日本時間の単体テスト（変更設計書 7.2・7.3 節）。"""

import unittest

from audit.common import contract_for, hour_key, model_matches, settle, validate_contracts

CONTRACTS = [
    {"id": "plus", "kind": "subscription", "currency": "USD", "models": ["gpt-5.6-terra", "review-judge"],
     "fees": {"2026-10": 20.0}},
    {"id": "max", "kind": "subscription", "currency": "USD", "models": ["anthropic/*", "claude-*"],
     "fees": {"2026-10": 100.0}},
    {"id": "azure", "kind": "invoice", "currency": "JPY", "models": ["azure-*"], "fees": {}},
]


def u(user, model, cost, category="request", email=None):
    return {"user_id": user, "user_email": email or (user or None), "model_group": model, "model": model,
            "category": category, "cost_usd": cost}


class MatchTest(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(model_matches("claude-*", "anthropic/*", "claude-sonnet-5"))   # model の方で当たる
        self.assertTrue(model_matches("anthropic/*", "anthropic/*", None))
        self.assertFalse(model_matches("gpt-5.6-terra", "gpt-5.6-terra-x", None))
        self.assertEqual(contract_for(CONTRACTS, "review-judge", "review-judge"), "plus")
        self.assertIsNone(contract_for(CONTRACTS, "fake-tools", "openai/fake-tools"))

    def test_validate(self):
        self.assertEqual(validate_contracts({"contracts": CONTRACTS}), [])
        bad = {"contracts": [CONTRACTS[0], {**CONTRACTS[1], "models": ["gpt-*"]}]}
        self.assertTrue(any("重なって" in e for e in validate_contracts(bad)))
        self.assertTrue(validate_contracts({"contracts": [{**CONTRACTS[0], "currency": "EUR"}]}))
        self.assertTrue(validate_contracts({"contracts": [{**CONTRACTS[0], "fees": {"2026/10": 1}}]}))

    def test_jst_boundary(self):
        self.assertEqual(hour_key("2026-10-11T14:59:59.999Z"), ("2026-10-11", "23"))
        self.assertEqual(hour_key("2026-10-11T15:00:00.000Z"), ("2026-10-12", "00"))


class SettleTest(unittest.TestCase):
    def rows(self, usage, closed=False, fx=150.0, contracts=CONTRACTS):
        return settle("2026-10", usage, contracts, fx, closed=closed)

    def test_allocation_sums_to_fee_and_follows_a(self):
        rows = self.rows([u("a", "gpt-5.6-terra", 3.0), u("b", "gpt-5.6-terra", 1.0),
                          u("a", "review-judge", 1.0, "review")])
        plus = [r for r in rows if r["contract_id"] == "plus"]
        self.assertAlmostEqual(sum(r["cost_b"] for r in plus), 20.0)
        a = next(r for r in plus if r["user_id"] == "a")
        self.assertEqual((a["cost_a_request"], a["cost_a_review"]), (3.0, 1.0))
        self.assertAlmostEqual(a["cost_b"], 16.0)      # (3 + 1) / 5 × 20
        self.assertEqual(a["status"], "estimate")
        self.assertEqual(sum(r["cost_b_jpy"] for r in plus), 3000)   # 20 USD × 150
        self.assertEqual(a["rate"], 4.0)

    def test_rounding_remainder_goes_to_largest(self):
        rows = self.rows([u("a", "gpt-5.6-terra", 1.0), u("b", "gpt-5.6-terra", 1.0), u("c", "gpt-5.6-terra", 1.0)])
        plus = sorted((r for r in rows if r["contract_id"] == "plus"), key=lambda r: r["user_id"])
        self.assertEqual([r["cost_b"] for r in plus], [6.66, 6.66, 6.68])   # 同じ重みなら名前の大きい方（決まった順）
        self.assertAlmostEqual(sum(r["cost_b"] for r in plus), 20.0)

    def test_no_user_unallocated_uncontracted(self):
        rows = self.rows([u(None, "gpt-5.6-terra", 1.0), u("x", "fake-tools", 0.5)])
        kinds = {(r["row_kind"], r["contract_id"]) for r in rows}
        self.assertIn(("no_user", "plus"), kinds)
        self.assertIn(("unallocated", "max"), kinds)          # 利用が無い月は未配分に実費を出す
        self.assertIn(("uncontracted", None), kinds)          # どの単位にも当たらないモデルは A だけ
        no_user = next(r for r in rows if r["row_kind"] == "no_user")
        self.assertAlmostEqual(no_user["cost_b"], 20.0)
        self.assertIsNone(no_user["user_id"])

    def test_status_and_fx(self):
        rows = self.rows([u("a", "gpt-5.6-terra", 1.0), u("a", "azure-gpt", 2.0)], closed=True, fx=None)
        plus = next(r for r in rows if r["contract_id"] == "plus")
        self.assertEqual((plus["status"], plus["fx_status"], plus["cost_b_jpy"]), ("final", "missing", None))
        azure = next(r for r in rows if r["contract_id"] == "azure")
        self.assertEqual((azure["status"], azure["cost_b"]), ("no_fee", None))   # 請求書の金額が未入力
        with_fee = [{**CONTRACTS[2], "fees": {"2026-10": 30000}}]
        az = settle("2026-10", [u("a", "azure-gpt", 2.0), u("b", "azure-gpt", 1.0)], with_fee, None, closed=True)
        self.assertEqual(sorted(r["cost_b"] for r in az), [10000, 20000])   # 円の単位は換算なし
        self.assertTrue(all(r["fx_status"] == "not_needed" for r in az))

    def test_invoice_open_month_has_no_b(self):
        c = [{**CONTRACTS[2], "fees": {"2026-10": 100}}]
        rows = settle("2026-10", [u("a", "azure-gpt", 1.0)], c, 150.0, closed=False)
        self.assertEqual(rows[0]["status"], "no_fee")   # 請求書の単位は月が締まるまで B を出さない


if __name__ == "__main__":
    unittest.main()
