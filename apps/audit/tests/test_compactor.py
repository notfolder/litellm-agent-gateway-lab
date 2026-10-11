"""整形（審査の結び付け）の単体テスト。"""

import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path

import duckdb

from audit.compactor import META_COLUMNS, flatten, parquet_source, usage_daily_select, write_parquet


class FlattenTest(unittest.TestCase):
    def test_reviews_join_requests_by_call_id(self):
        rows = [
            {"type": "request", "call_id": "c1", "user_email": "a@x", "tokens_in": 5,
             "tool_calls": [{"name": "shell", "args_head": "rm"}], "body": {"key": "k", "offset": 0, "length": 9}},
            {"type": "review", "request_call_id": "c1", "allowed": False, "reason": "削除",
             "judge_model_group": "review-judge", "judge_tokens_in": 100, "judge_cost_usd": 0.01, "jev_probability": 0.9},
            {"type": "review", "request_call_id": "c1", "allowed": True, "judge_cost_usd": 0.02},
            {"type": "mcp", "call_id": "c2", "mcp_tool": "list_labels"},
            {"type": "review", "request_call_id": "missing"},
        ]
        out = {r["call_id"]: r for r in flatten(rows)}
        self.assertEqual(set(out), {"c1", "c2"})
        c1 = out["c1"]
        self.assertEqual((c1["review_count"], c1["review_blocked"]), (2, True))
        self.assertAlmostEqual(c1["judge_cost_usd"], 0.03)
        self.assertEqual(c1["judge_model_group"], "review-judge")
        self.assertEqual(c1["tool_names"], "shell")
        self.assertEqual((c1["body_key"], c1["body_offset"], c1["body_length"]), ("k", 0, 9))
        self.assertEqual(out["c2"]["review_count"], 0)


class ParquetTest(unittest.TestCase):
    def test_write_and_read_back_sorted_by_email(self):
        rows = flatten([
            {"type": "request", "call_id": "b", "user_email": "z@x", "ts_start": "2026-10-11T15:00:01.000Z"},
            {"type": "request", "call_id": "a", "user_email": "a@x", "ts_start": "2026-10-11T15:00:02.000Z"},
            {"type": "request", "call_id": "n", "user_email": None, "ts_start": "2026-10-11T15:00:03.000Z"},
        ])
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "m.parquet"
            con = duckdb.connect()
            write_parquet(con, rows, dest, META_COLUMNS, "user_email NULLS FIRST, ts_start")
            # TIMESTAMPTZ を Python の値にするには pytz が要る（無いと検索アプリが動かない）
            got = con.execute(f"SELECT call_id, ts_start FROM read_parquet('{dest}')").fetchall()
            self.assertEqual([g[0] for g in got], ["n", "a", "b"])
            self.assertEqual(got[1][1], datetime(2026, 10, 11, 15, 0, 2, tzinfo=timezone.utc))   # 同じ時刻
            empty = Path(d) / "e.parquet"
            write_parquet(con, [], empty, META_COLUMNS, "user_email")
            self.assertEqual(con.execute(f"SELECT count(*) FROM read_parquet('{empty}')").fetchone()[0], 0)


class IsolatedReviewTest(unittest.TestCase):
    """文脈独立審査（review_kind: isolated）の記録の整形と、審査官ごとの利用量。"""

    def rows(self):
        return [
            {"type": "request", "call_id": "c1", "user_id": "u1", "user_email": "a@x", "client": "codex",
             "model_group": "fake-tools", "ts_start": "2026-10-12T01:00:00.000Z", "cost_usd": 1.0},
            {"type": "review", "review_kind": "context", "request_call_id": "c1", "allowed": True, "reason": "問題なし",
             "judge_model_group": "review-judge", "judge_tokens_in": 10, "judge_cost_usd": 0.1},
            {"type": "review", "review_kind": "isolated", "request_call_id": "c1", "allowed": False, "reason": "キー",
             "judge_model_group": "isolated-judge", "judge_tokens_in": 20, "judge_tokens_out": 3,
             "judge_cost_usd": 0.2},
            # Claude Code（文脈独立審査だけ）
            {"type": "request", "call_id": "c2", "user_id": "u1", "user_email": "a@x", "client": "claude-code",
             "model_group": "anthropic/x", "ts_start": "2026-10-12T01:00:01.000Z", "cost_usd": 0.5},
            {"type": "review", "review_kind": "isolated", "request_call_id": "c2", "allowed": True, "reason": "なし",
             "judge_model_group": "isolated-judge", "judge_cost_usd": 0.3},
        ]

    def test_flatten_splits_by_kind(self):
        out = {r["call_id"]: r for r in flatten(self.rows())}
        c1 = out["c1"]
        self.assertEqual((c1["review_count"], c1["context_blocked"], c1["review_reason"]), (1, False, "問題なし"))
        self.assertEqual((c1["isolated_count"], c1["isolated_blocked"], c1["isolated_reason"]), (1, True, "キー"))
        self.assertTrue(c1["review_blocked"])                     # どちらかが止めたら止めた
        self.assertEqual((c1["judge_model_group"], c1["judge_cost_usd"]), ("review-judge", 0.1))
        self.assertEqual((c1["isolated_judge_model_group"], c1["isolated_cost_usd"]), ("isolated-judge", 0.2))
        c2 = out["c2"]
        self.assertEqual((c2["review_count"], c2["context_blocked"], c2["isolated_count"]), (0, None, 1))
        self.assertFalse(c2["review_blocked"])

    def test_record_without_kind_is_context(self):
        out = flatten([{"type": "request", "call_id": "c"},
                       {"type": "review", "request_call_id": "c", "allowed": False, "judge_model_group": "review-judge"}])
        self.assertEqual((out[0]["review_count"], out[0]["context_blocked"], out[0]["isolated_count"]), (1, True, 0))

    def test_daily_usage_splits_judges_and_reads_old_files(self):
        with tempfile.TemporaryDirectory() as d:
            con = duckdb.connect()
            new = Path(d) / "new.parquet"
            write_parquet(con, flatten(self.rows()), new, META_COLUMNS, "ts_start")
            # 文脈独立審査の列を足す前の形のファイル
            old_cols = {k: v for k, v in META_COLUMNS.items() if not (k == "context_blocked" or k.startswith("isolated_"))}
            old = Path(d) / "old.parquet"
            write_parquet(con, flatten([
                {"type": "request", "call_id": "o1", "user_id": "u1", "user_email": "a@x", "client": "codex",
                 "model_group": "fake-tools", "ts_start": "2026-10-12T00:00:00.000Z", "cost_usd": 2.0},
                {"type": "review", "request_call_id": "o1", "allowed": True, "judge_model_group": "review-judge",
                 "judge_cost_usd": 0.4}]), old, old_cols, "ts_start")

            def usage(files):
                cur = con.execute(usage_daily_select("2026-10-12", parquet_source(con, [str(f) for f in files])))
                names = [x[0] for x in cur.description]
                return [dict(zip(names, r)) for r in cur.fetchall()]

            got: dict = {}
            for r in usage([new, old]):    # 行は利用者・モデル・クライアントごと。審査官ごとに足す
                g = got.setdefault((r["category"], r["model_group"]), {"cost_usd": 0.0, "requests": 0})
                g["cost_usd"] += r["cost_usd"]
                g["requests"] += r["requests"]
            self.assertAlmostEqual(got[("review", "review-judge")]["cost_usd"], 0.5)
            self.assertAlmostEqual(got[("review", "isolated-judge")]["cost_usd"], 0.5)
            self.assertEqual(got[("review", "isolated-judge")]["requests"], 2)
            # 古いファイルだけでも集計できる（新しい列は空として補う）
            only_old = {(r["category"], r["model_group"]) for r in usage([old])}
            self.assertEqual(only_old, {("request", "fake-tools"), ("review", "review-judge")})


if __name__ == "__main__":
    unittest.main()
