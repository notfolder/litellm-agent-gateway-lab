"""整形（審査の結び付け）の単体テスト。"""

import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path

import duckdb

from audit.compactor import META_COLUMNS, flatten, write_parquet


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


if __name__ == "__main__":
    unittest.main()
