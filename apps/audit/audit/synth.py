"""検索の速さの試験用の合成データ（変更設計書 5.9 節）。python -m audit.synth --days 7 --per-day 1000000

audit-derived/synth/ に、時間ごとの Parquet と日次の集計だけを作る（本文と原本は作らない）。
利用者 15,000 人。1 人あたりの件数はべき分布（上位 10% が約 60%）。モデル 4 種、クライアント 2 種、審査は 10%。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from .common import DERIVED, Config, now_jst

USERS = 15000


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m audit.synth")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--per-day", type=int, default=1_000_000)
    args = ap.parse_args(argv)
    import duckdb
    cfg = Config.from_env()
    s3 = cfg.s3()
    con = duckdb.connect()
    # スレッドを絞り、並べ替えが溢れたら一時ファイルに書く（既定のスレッド数では 1Gi を超えて落ちた）
    con.execute("SET memory_limit='512MB'")
    con.execute("SET threads=2")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{tempfile.gettempdir()}/duckdb-spill'")
    tmp = Path(tempfile.mkdtemp(prefix="synth-"))
    end = now_jst().date() - timedelta(days=1)
    per_hour = args.per_day // 24
    t0 = time.monotonic()
    for i in range(args.days):
        day = end - timedelta(days=args.days - 1 - i)
        for hour in range(24):
            dest = tmp / "h.parquet"
            # 利用者の番号は (乱数^4) で偏らせる（上位の利用者に件数が集まる）
            con.execute(f"""COPY (
              SELECT md5(random()::VARCHAR) AS call_id, 'request' AS type,
                ('{day} {hour:02d}:00:00+09'::TIMESTAMPTZ + to_seconds(floor(random() * 3600)::BIGINT)) AS ts_start,
                NULL::TIMESTAMPTZ AS ts_end,
                'user' || u || '@example.com' AS user_id, 'user' || u || '@example.com' AS user_email,
                'team' || (u % 50) AS team_id, NULL AS team_alias, 'key' || u AS key_alias,
                CASE WHEN random() < 0.5 THEN 'codex' ELSE 'claude-code' END AS client, NULL AS session_id,
                'acompletion' AS call_type, m AS model_group, m AS model, 'success' AS status, NULL AS error_class,
                (random() * 50000)::BIGINT AS tokens_in, (random() * 2000)::BIGINT AS tokens_out,
                (random() * 30000)::BIGINT AS tokens_cache_read, 0::BIGINT AS tokens_cache_write,
                random() * 0.05 AS cost_usd, (random() * 20000)::BIGINT AS latency_ms, NULL::BIGINT AS ttft_ms,
                CASE WHEN random() < 0.3 THEN 'shell' END AS tool_names, NULL AS tool_calls, NULL AS mcp_server,
                NULL AS mcp_tool, NULL AS body_key, NULL::BIGINT AS body_offset, NULL::BIGINT AS body_length,
                NULL::BIGINT AS body_bytes,
                CASE WHEN r < 0.1 THEN 1 ELSE 0 END AS review_count,
                CASE WHEN r < 0.01 THEN true WHEN r < 0.1 THEN false END AS review_blocked,
                NULL AS review_reason, NULL::DOUBLE AS jev_probability,
                CASE WHEN r < 0.1 THEN 'review-judge' END AS judge_model_group, 0::BIGINT AS judge_tokens_in,
                0::BIGINT AS judge_tokens_out, CASE WHEN r < 0.1 THEN random() * 0.001 ELSE 0 END AS judge_cost_usd,
                'synth' AS pod,
                -- 文脈独立審査の列（合成データでは行わなかったものとして、空と 0）
                CASE WHEN r < 0.01 THEN true WHEN r < 0.1 THEN false END AS context_blocked,
                0::BIGINT AS isolated_count, NULL::BOOLEAN AS isolated_blocked, NULL::VARCHAR AS isolated_reason,
                NULL::VARCHAR AS isolated_judge_model_group, 0::BIGINT AS isolated_tokens_in,
                0::BIGINT AS isolated_tokens_out, 0::DOUBLE AS isolated_cost_usd
              FROM (SELECT (floor(pow(random(), 4) * {USERS}))::BIGINT AS u,
                           ['gpt-5.6-terra', 'gpt-5.6-luna', 'anthropic/claude-sonnet', 'anthropic/claude-opus'][1 + floor(random() * 4)::INT] AS m,
                           random() AS r
                    FROM range({per_hour}))
              ORDER BY user_email, ts_start
            ) TO '{dest}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)""")
            s3.upload_file(str(dest), DERIVED, f"synth/meta/dt={day}/hour={hour:02d}/part-0.parquet")
        # 日ごとの整形済みのデータと日次の集計（その日の 24 時間分から。整形の処理と同じ形）
        src = f"read_parquet('s3://{DERIVED}/synth/meta/dt={day}/hour=*/part-0.parquet')"
        con.execute("LOAD httpfs")
        con.execute(cfg.duckdb_secret_sql())
        day_file = tmp / "day.parquet"
        con.execute(f"COPY (SELECT * FROM {src} ORDER BY user_email NULLS FIRST, ts_start) TO '{day_file}' "
                    f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
        s3.upload_file(str(day_file), DERIVED, f"synth/meta_day/dt={day}/part-0.parquet")
        dest = tmp / "d.parquet"
        con.execute(f"""COPY (
            SELECT '{day}'::DATE AS day, user_id, any_value(user_email) AS user_email, team_id, model_group, model,
                   client, 'request' AS category, count(*) AS requests, sum(tokens_in) AS tokens_in,
                   sum(tokens_out) AS tokens_out, sum(tokens_cache_read) AS tokens_cache_read,
                   sum(tokens_cache_write) AS tokens_cache_write, sum(cost_usd) AS cost_usd
            FROM {src} GROUP BY ALL ORDER BY user_id) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)""")
        s3.upload_file(str(dest), DERIVED, f"synth/usage_daily/dt={day}/part-0.parquet")
        print(f"synth: {day} を作りました（{per_hour * 24:,} 件、経過 {int(time.monotonic() - t0)} 秒）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
