"""整形の定期処理（変更設計書 5.8 節）。CronJob で毎時 10 分に動く（python -m audit.compactor）。

1. 時間 H の原本（raw/meta）を、H と H+1 が締まってから整形する。要求に審査を call_id で結び付け、利用者と時刻の
   順に並べて Parquet（meta/dt=…/hour=…/part-0.parquet）に書く
2. 日本時間の日の全時間を整形したら、日次の集計（usage_daily）を作る
3. 当月の精算を作り直す。前月までは、日次の集計・請求の単位・換算のレートのどれかが変わった月だけ作り直す
原本のファイルの数と最終更新時刻を _state/compactor.json に持ち、変わった時間だけを処理し直す（遅れて届いた記録も拾う）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import (
    DERIVED, JST, RAW, Config, hour_start, latest_json, list_keys, next_hour, now_jst, put_json, read_raw_hour,
    settle,
)

# Parquet の列（型を固定する。空の時間でも同じ形になるように）
META_COLUMNS = {
    "call_id": "VARCHAR", "type": "VARCHAR", "ts_start": "TIMESTAMPTZ", "ts_end": "TIMESTAMPTZ",
    "user_id": "VARCHAR", "user_email": "VARCHAR", "team_id": "VARCHAR", "team_alias": "VARCHAR",
    "key_alias": "VARCHAR", "client": "VARCHAR", "session_id": "VARCHAR", "call_type": "VARCHAR",
    "model_group": "VARCHAR", "model": "VARCHAR", "status": "VARCHAR", "error_class": "VARCHAR",
    "tokens_in": "BIGINT", "tokens_out": "BIGINT", "tokens_cache_read": "BIGINT", "tokens_cache_write": "BIGINT",
    "cost_usd": "DOUBLE", "latency_ms": "BIGINT", "ttft_ms": "BIGINT", "tool_names": "VARCHAR",
    "tool_calls": "VARCHAR", "mcp_server": "VARCHAR", "mcp_tool": "VARCHAR",
    "body_key": "VARCHAR", "body_offset": "BIGINT", "body_length": "BIGINT", "body_bytes": "BIGINT",
    "review_count": "BIGINT", "review_blocked": "BOOLEAN", "review_reason": "VARCHAR", "jev_probability": "DOUBLE",
    "judge_model_group": "VARCHAR", "judge_tokens_in": "BIGINT", "judge_tokens_out": "BIGINT",
    "judge_cost_usd": "DOUBLE", "pod": "VARCHAR",
    # 文脈独立審査（機密情報の持ち出し）。review_* と judge_* は文脈あり審査だけの値（review_blocked は両方）
    "context_blocked": "BOOLEAN", "isolated_count": "BIGINT", "isolated_blocked": "BOOLEAN",
    "isolated_reason": "VARCHAR", "isolated_judge_model_group": "VARCHAR", "isolated_tokens_in": "BIGINT",
    "isolated_tokens_out": "BIGINT", "isolated_cost_usd": "DOUBLE",
}
# 文脈独立審査を足す前の Parquet には無い列と、無いときに補う値
ADDED_COLUMNS = {"context_blocked": "NULL::BOOLEAN", "isolated_count": "0::BIGINT",
                 "isolated_blocked": "NULL::BOOLEAN", "isolated_reason": "NULL::VARCHAR",
                 "isolated_judge_model_group": "NULL::VARCHAR", "isolated_tokens_in": "0::BIGINT",
                 "isolated_tokens_out": "0::BIGINT", "isolated_cost_usd": "0::DOUBLE"}
STATE_KEY = "_state/compactor.json"


def parquet_source(con, files: list[str]) -> str:
    """Parquet の一覧を読む SELECT 文の FROM 句。列の名前で合わせて読み（古いファイルと新しいファイルが混ざるため）、
    どのファイルにも無い新しい列は空の値で補う（全部が古いファイルのときに、列を名指しした SQL が失敗しないように）。"""
    src = "read_parquet([" + ", ".join(f"'{p}'" for p in files) + "], union_by_name=true)"
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    missing = [f"{v} AS {k}" for k, v in ADDED_COLUMNS.items() if k not in have]
    return f"(SELECT *{', ' + ', '.join(missing) if missing else ''} FROM {src})" if missing else src


def flatten(rows: list[dict]) -> list[dict]:
    """原本の行（要求・MCP・審査）を、整形済みの行（要求ごと。審査の結果を足したもの）にする。
    審査は review_kind で分ける（無いものは、文脈独立審査を足す前の記録なので文脈あり審査）。"""
    reviews: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("type") == "review" and r.get("request_call_id"):
            reviews.setdefault(r["request_call_id"], []).append(r)
    out = []
    for r in rows:
        if r.get("type") not in ("request", "mcp"):
            continue
        allrv = reviews.get(r.get("call_id") or "", [])
        rv = [x for x in allrv if (x.get("review_kind") or "context") == "context"]
        iv = [x for x in allrv if x.get("review_kind") == "isolated"]
        body = r.get("body") or {}
        tools = r.get("tool_calls") or []
        out.append({
            **{k: r.get(k) for k in META_COLUMNS if k in r and k not in ("tool_calls",)},
            "tool_names": ",".join(t.get("name") or "" for t in tools) or None,
            "tool_calls": json.dumps(tools, ensure_ascii=False) if tools else None,
            "body_key": body.get("key"), "body_offset": body.get("offset"), "body_length": body.get("length"),
            "review_count": len(rv),
            "review_blocked": any(x.get("allowed") is False for x in allrv) if allrv else None,
            "context_blocked": any(x.get("allowed") is False for x in rv) if rv else None,
            "review_reason": rv[-1].get("reason") if rv else None,
            "jev_probability": max((x.get("jev_probability") or 0.0) for x in rv) if rv else None,
            "judge_model_group": next((x.get("judge_model_group") for x in rv if x.get("judge_model_group")), None),
            "judge_tokens_in": sum(int(x.get("judge_tokens_in") or 0) for x in rv),
            "judge_tokens_out": sum(int(x.get("judge_tokens_out") or 0) for x in rv),
            "judge_cost_usd": sum(float(x.get("judge_cost_usd") or 0) for x in rv),
            "isolated_count": len(iv),
            "isolated_blocked": any(x.get("allowed") is False for x in iv) if iv else None,
            "isolated_reason": iv[-1].get("reason") if iv else None,
            "isolated_judge_model_group": next((x.get("judge_model_group") for x in iv
                                                if x.get("judge_model_group")), None),
            "isolated_tokens_in": sum(int(x.get("judge_tokens_in") or 0) for x in iv),
            "isolated_tokens_out": sum(int(x.get("judge_tokens_out") or 0) for x in iv),
            "isolated_cost_usd": sum(float(x.get("judge_cost_usd") or 0) for x in iv),
        })
    return out


def write_parquet(con, rows: list[dict], dest: Path, columns: dict[str, str], order: str) -> None:
    """行を Parquet に書く（行グループ 10 万行、zstd）。"""
    src = dest.with_suffix(".ndjson")
    src.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows))
    cols = ", ".join(f"'{k}': '{v}'" for k, v in columns.items())
    select = f"SELECT * FROM read_json('{src}', format='newline_delimited', columns={{{cols}}})" if rows else \
        "SELECT " + ", ".join(f"NULL::{v} AS {k}" for k, v in columns.items()) + " WHERE false"
    con.execute(f"COPY ({select} ORDER BY {order}) TO '{dest}' "
                f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    src.unlink(missing_ok=True)


def usage_daily_select(day: str, src: str) -> str:
    """日ごとの利用量の SELECT 文（src は parquet_source の戻り値）。要求の分と、審査の分を審査官ごとに分けた行
    （文脈あり審査は judge_model_group、文脈独立審査は isolated_judge_model_group）。"""
    return f"""
                SELECT '{day}'::DATE AS day, user_id, any_value(user_email) AS user_email, team_id, model_group, model,
                       client, 'request' AS category, count(*) AS requests, sum(tokens_in) AS tokens_in,
                       sum(tokens_out) AS tokens_out, sum(tokens_cache_read) AS tokens_cache_read,
                       sum(tokens_cache_write) AS tokens_cache_write, sum(cost_usd) AS cost_usd
                FROM {src} GROUP BY ALL
                UNION ALL
                SELECT '{day}'::DATE, user_id, any_value(user_email), team_id, judge_model_group, judge_model_group,
                       client, 'review', sum(review_count), sum(judge_tokens_in), sum(judge_tokens_out), 0, 0,
                       sum(judge_cost_usd)
                FROM {src} WHERE review_count > 0 AND judge_model_group IS NOT NULL
                GROUP BY user_id, team_id, judge_model_group, client
                UNION ALL
                SELECT '{day}'::DATE, user_id, any_value(user_email), team_id, isolated_judge_model_group,
                       isolated_judge_model_group, client, 'review', sum(isolated_count), sum(isolated_tokens_in),
                       sum(isolated_tokens_out), 0, 0, sum(isolated_cost_usd)
                FROM {src} WHERE isolated_count > 0 AND isolated_judge_model_group IS NOT NULL
                GROUP BY user_id, team_id, isolated_judge_model_group, client
                ORDER BY user_id NULLS FIRST"""


class Compactor:
    def __init__(self, cfg: Config) -> None:
        import duckdb
        self.cfg = cfg
        self.s3 = cfg.s3()
        self.con = duckdb.connect()
        self.con.execute(f"SET memory_limit='{os.environ.get('AUDIT_DUCKDB_MEMORY', '512MB')}'")
        # スレッドを絞り、並べ替えが溢れたら一時ファイルに書く（日ごとのファイルにまとめるとき、本番の規模では
        # 1 日分の並べ替えになるため）
        self.con.execute(f"SET threads={int(os.environ.get('AUDIT_DUCKDB_THREADS', '2'))}")
        self.con.execute("SET preserve_insertion_order=false")
        self.con.execute(f"SET temp_directory='{tempfile.gettempdir()}/duckdb-spill'")
        self.tmp = Path(tempfile.mkdtemp(prefix="compactor-"))
        self.prefix = cfg.data_prefix

    # ---- 状態 ----
    def load_state(self) -> dict:
        try:
            return json.loads(self.s3.get_object(**DERIVED.ref(self.prefix + STATE_KEY))["Body"].read())
        except Exception:  # noqa: BLE001 初回
            return {"hours": {}, "days": {}, "contracts_version": None, "fx_version": None, "runs": []}

    def save_state(self, state: dict) -> None:
        put_json(self.s3, DERIVED, self.prefix + STATE_KEY, state)

    def raw_hours(self) -> dict[str, str]:
        """原本の時間ごとの「ファイル数:最終更新時刻」（変わったら処理し直す）。"""
        sig: dict[str, list] = {}
        for o in list_keys(self.s3, RAW, "raw/meta/"):
            parts = o["Key"].split("/")
            dt, hour = parts[2].removeprefix("dt="), parts[3].removeprefix("hour=")
            n, last = sig.get(f"{dt} {hour}", [0, ""])
            sig[f"{dt} {hour}"] = [n + 1, max(last, o["LastModified"].isoformat())]
        return {k: f"{n}:{last}" for k, (n, last) in sig.items()}

    # ---- 時間 ----
    def process_hour(self, dt: str, hour: str) -> int:
        rows = read_raw_hour(self.s3, dt, hour)
        ndt, nhour = next_hour(dt, hour)
        rows += [r for r in read_raw_hour(self.s3, ndt, nhour) if r.get("type") == "review"]
        flat = flatten(rows)
        dest = self.tmp / f"meta-{dt}-{hour}.parquet"
        write_parquet(self.con, flat, dest, META_COLUMNS, "user_email NULLS FIRST, ts_start")
        self.s3.upload_file(str(dest), DERIVED.bucket, DERIVED.key(f"{self.prefix}meta/dt={dt}/hour={hour}/part-0.parquet"))
        dest.unlink(missing_ok=True)
        return len(flat)

    # ---- 日 ----
    def process_day(self, day: str) -> None:
        files = [o["Key"] for o in list_keys(self.s3, DERIVED, f"{self.prefix}meta/dt={day}/")]
        local = []
        for i, key in enumerate(files):
            p = self.tmp / f"day-{day}-{i}.parquet"
            self.s3.download_file(DERIVED.bucket, DERIVED.key(key), str(p))
            local.append(str(p))
        dest = self.tmp / f"usage-{day}.parquet"
        if local:
            # 日ごとの整形済みのデータ（時間ごとのファイルを 1 つにまとめ、利用者のメールアドレスと時刻の順に並べる）。
            # 90 日分の履歴を、2,160 個ではなく 90 個のファイルで読めるようにする（2026-10-12 の速さの試験の見積もり）
            day_file = self.tmp / f"meta-day-{day}.parquet"
            src_all = parquet_source(self.con, local)
            self.con.execute(f"COPY (SELECT * FROM {src_all} ORDER BY user_email NULLS FIRST, ts_start) TO '{day_file}' "
                             f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
            self.s3.upload_file(str(day_file), DERIVED.bucket, DERIVED.key(f"{self.prefix}meta_day/dt={day}/part-0.parquet"))
            day_file.unlink(missing_ok=True)
            src = src_all
            self.con.execute(f"COPY ({usage_daily_select(day, src)}\n) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)")
        else:
            self.con.execute(f"""COPY (SELECT NULL::DATE AS day, NULL::VARCHAR AS user_id, NULL::VARCHAR AS user_email,
                NULL::VARCHAR AS team_id, NULL::VARCHAR AS model_group, NULL::VARCHAR AS model, NULL::VARCHAR AS client,
                NULL::VARCHAR AS category, 0::BIGINT AS requests, 0::HUGEINT AS tokens_in, 0::HUGEINT AS tokens_out,
                0::HUGEINT AS tokens_cache_read, 0::HUGEINT AS tokens_cache_write, 0::DOUBLE AS cost_usd WHERE false)
                TO '{dest}' (FORMAT parquet)""")
        self.s3.upload_file(str(dest), DERIVED.bucket, DERIVED.key(f"{self.prefix}usage_daily/dt={day}/part-0.parquet"))
        for p in local + [str(dest)]:
            Path(p).unlink(missing_ok=True)

    # ---- 月 ----
    def usage_rows(self, month: str) -> list[dict]:
        files = [o["Key"] for o in list_keys(self.s3, DERIVED, f"{self.prefix}usage_daily/dt={month}-")]
        rows: list[dict] = []
        for i, key in enumerate(files):
            p = self.tmp / f"u-{i}.parquet"
            self.s3.download_file(DERIVED.bucket, DERIVED.key(key), str(p))
            # 利用者 × モデル × 種類に集計してから持ち込む（日次の集計の行をそのまま持ち込むと、メモリを使いすぎる）
            cur = self.con.execute(f"SELECT user_id, any_value(user_email) AS user_email, model_group, model, category, "
                                   f"sum(cost_usd) AS cost_usd FROM read_parquet('{p}') GROUP BY ALL")
            names = [d[0] for d in cur.description]
            rows += [dict(zip(names, r)) for r in cur.fetchall()]
            p.unlink(missing_ok=True)
        return rows

    def process_month(self, month: str, contracts: tuple[str | None, dict | None],
                      fx: tuple[str | None, dict | None]) -> int:
        today = now_jst().date()
        closed = month < today.strftime("%Y-%m")
        rate = ((fx[1] or {}).get("usd_jpy") or {}).get(month)
        rows = settle(month, self.usage_rows(month), (contracts[1] or {}).get("contracts") or [], rate,
                      closed=closed, contracts_version=contracts[0], fx_version=fx[0],
                      computed_at=datetime.now(JST).isoformat(timespec="seconds"))
        dest = self.tmp / f"settlement-{month}.ndjson"
        dest.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        out = self.tmp / f"settlement-{month}.parquet"
        if rows:
            self.con.execute(f"COPY (SELECT * FROM read_json('{dest}', format='newline_delimited')) TO '{out}' "
                             f"(FORMAT parquet)")
        else:
            self.con.execute(f"COPY (SELECT NULL::VARCHAR AS month WHERE false) TO '{out}' (FORMAT parquet)")
        self.s3.upload_file(str(out), DERIVED.bucket, DERIVED.key(f"{self.prefix}billing/settlement/month={month}/part-0.parquet"))
        dest.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return len(rows)

    # ---- 全体 ----
    def run(self, only_hour: tuple[str, str] | None = None) -> dict:
        t0 = time.monotonic()
        state = self.load_state()
        now = now_jst()
        sig = self.raw_hours()
        hours_done: list[str] = []
        for key in sorted(sig):
            dt, hour = key.split()
            if only_hour and (dt, hour) != only_hour:
                continue
            # H と H+1 が締まってから（審査の記録は後の時間に入ることがあるため）。--hour のときは今すぐ
            closed = hour_start(dt, hour) + timedelta(hours=2) <= now
            nkey = " ".join(next_hour(dt, hour))
            want = f"{sig[key]}|{sig.get(nkey, '')}"
            if (closed or only_hour) and state["hours"].get(key) != want:
                n = self.process_hour(dt, hour)
                state["hours"][key] = want
                hours_done.append(key)
                print(f"compactor: {key} を整形しました（{n} 件）", flush=True)
        days_done = []
        for day in sorted({k.split()[0] for k in hours_done}):
            if date.fromisoformat(day) < now.date() or only_hour:
                self.process_day(day)
                state["days"][day] = now.isoformat(timespec="seconds")
                days_done.append(day)
                print(f"compactor: {day} の日次の集計を作りました", flush=True)
        contracts = latest_json(self.s3, DERIVED, "billing/contracts/")
        fx = latest_json(self.s3, DERIVED, "billing/fx_rates/")
        changed_defs = (contracts[0] != state.get("contracts_version") or fx[0] != state.get("fx_version"))
        months = {now.strftime("%Y-%m")} | {d[:7] for d in days_done}
        if changed_defs:
            months |= {d[:7] for d in state["days"]}
        for month in sorted(months):
            n = self.process_month(month, contracts, fx)
            print(f"compactor: {month} の精算を作りました（{n} 行）", flush=True)
        state["contracts_version"], state["fx_version"] = contracts[0], fx[0]
        run = {"at": now.isoformat(timespec="seconds"), "hours": len(hours_done), "days": len(days_done),
               "months": sorted(months), "seconds": round(time.monotonic() - t0, 1)}
        state["runs"] = (state.get("runs") or [])[-23:] + [run]
        state["last_processed_hour"] = max(state["hours"]) if state["hours"] else None
        self.save_state(state)
        print("compactor: " + json.dumps(run, ensure_ascii=False), flush=True)
        return run


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m audit.compactor")
    ap.add_argument("--hour", help="この時間（日本時間、YYYY-MM-DDTHH）だけを、締まるのを待たずに整形する")
    args = ap.parse_args(argv)
    only = tuple(args.hour.split("T")) if args.hour else None
    Compactor(Config.from_env()).run(only)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
