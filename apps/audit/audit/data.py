"""検索アプリのデータの読み書き（変更設計書 6.4 節）。

- 利用量: 日次の集計（usage_daily）がある日はそれを、無い日は時間ごとの Parquet と、まだ整形していない原本から集計する
- 履歴: 時間ごとの Parquet（user_email と時刻の順に並べてある）を DuckDB で読み、まだ整形していない時間は原本を読む
- 本文: 記録の body で範囲指定の取得をする
すべての読み取りに、役割による絞り込み（一般の利用者は自分の user_email だけ）を呼ぶ側がかける。
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from .common import (
    ACCESS, DERIVED, JST, RAW, Config, duckdb_connect, hour_key, latest_json, list_keys, now_jst, parse_iso,
    put_json, read_body, read_ndjson_gz, read_raw_hour, retention_args,
)
from .compactor import flatten

USAGE_KEYS = ("user_id", "user_email", "model_group", "client", "category", "day")


class Store:
    def __init__(self, cfg: Config, pod: str = "viewer") -> None:
        self.cfg = cfg
        self.s3 = cfg.s3()
        self.pod = pod
        self.prefix = cfg.data_prefix

    def con(self):
        return duckdb_connect(self.cfg)

    def url(self, key: str) -> str:
        return DERIVED.url(key)

    # ---- 一覧 ----
    def _derived_keys(self, kind: str, days: list[date]) -> dict[str, list[str]]:
        """日ごとの整形済みのファイル（kind は meta か usage_daily）。"""
        out: dict[str, list[str]] = {}
        for d in days:
            keys = [o["Key"] for o in list_keys(self.s3, DERIVED, f"{self.prefix}{kind}/dt={d.isoformat()}/")]
            if keys:
                out[d.isoformat()] = sorted(keys)
        return out

    def _hours(self, start: datetime, end: datetime) -> list[tuple[str, str]]:
        hours, t = [], start.astimezone(JST).replace(minute=0, second=0, microsecond=0)
        while t <= end:
            hours.append((t.strftime("%Y-%m-%d"), t.strftime("%H")))
            t += timedelta(hours=1)
        return hours

    def _raw_rows(self, hours: list[tuple[str, str]], have: set[tuple[str, str]]) -> list[dict]:
        """まだ整形していない時間の原本（要求ごとに審査を結び付けた形）。合成データのときは読まない。"""
        if self.prefix:
            return []
        rows: list[dict] = []
        for dt, hour in hours:
            if (dt, hour) not in have:
                rows += read_raw_hour(self.s3, dt, hour)
        return flatten(rows)

    # ---- 利用量 ----
    def usage(self, start: date, end: date, email: str | None, group_by: list[str]) -> list[dict]:
        """利用量と概算の集計。日次の集計がある日は DuckDB の中で集計し（行を Python に持ち込まない）、
        まだ無い日（今日など）は時間ごとのデータから集計して足す。"""
        group_by = [g for g in group_by if g in USAGE_KEYS] or ["user_email"]
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        daily = self._derived_keys("usage_daily", days)
        sums = ("requests", "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "cost_usd")
        agg: dict[tuple, dict] = defaultdict(lambda: {k: 0 for k in sums})
        if daily:
            files = [self.url(k) for ks in daily.values() for k in ks]
            where, params = ("WHERE user_email = ?", [email]) if email else ("", [])
            cols = [("strftime(day, '%Y-%m-%d')" if g == "day" else g) + f" AS {g}" for g in group_by]
            cur = self.con().execute(
                f"SELECT {', '.join(cols)}, " + ", ".join(f"sum({k}) AS {k}" for k in sums)
                + f" FROM read_parquet({files!r}) {where} GROUP BY ALL", params)
            for row in cur.fetchall():
                key, vals = tuple(row[:len(group_by)]), row[len(group_by):]
                for k, v in zip(sums, vals):
                    agg[key][k] += float(v or 0) if k == "cost_usd" else int(v or 0)
        # 日次の集計がまだ無い日（今日、締まっていない日）は、時間ごとのデータから集計する
        for d in days:
            if d.isoformat() in daily:
                continue
            day_start = datetime(d.year, d.month, d.day, tzinfo=JST)
            for r in self._detail_rows(day_start, day_start + timedelta(hours=23, minutes=59), email):
                base = {"day": d.isoformat(), "user_id": r.get("user_id"), "user_email": r.get("user_email"),
                        "client": r.get("client")}
                parts = [base | {"model_group": r.get("model_group"), "category": "request", "requests": 1,
                                 "tokens_in": r.get("tokens_in") or 0, "tokens_out": r.get("tokens_out") or 0,
                                 "tokens_cache_read": r.get("tokens_cache_read") or 0,
                                 "tokens_cache_write": r.get("tokens_cache_write") or 0,
                                 "cost_usd": r.get("cost_usd") or 0.0}]
                # 審査の分は審査官ごとに分ける（文脈あり審査と文脈独立審査）
                if r.get("isolated_count") and r.get("isolated_judge_model_group"):
                    parts.append(base | {"model_group": r["isolated_judge_model_group"], "category": "review",
                                         "requests": r["isolated_count"],
                                         "tokens_in": r.get("isolated_tokens_in") or 0,
                                         "tokens_out": r.get("isolated_tokens_out") or 0, "tokens_cache_read": 0,
                                         "tokens_cache_write": 0, "cost_usd": r.get("isolated_cost_usd") or 0.0})
                if r.get("review_count") and r.get("judge_model_group"):
                    parts.append(base | {"model_group": r["judge_model_group"], "category": "review",
                                         "requests": r["review_count"], "tokens_in": r.get("judge_tokens_in") or 0,
                                         "tokens_out": r.get("judge_tokens_out") or 0, "tokens_cache_read": 0,
                                         "tokens_cache_write": 0, "cost_usd": r.get("judge_cost_usd") or 0.0})
                for x in parts:
                    a = agg[tuple(x.get(g) for g in group_by)]
                    for k in sums:
                        a[k] += x.get(k) or 0
        out = [dict(zip(group_by, k)) | v for k, v in agg.items()]
        return sorted(out, key=lambda x: (-x["cost_usd"], str(x)))

    # ---- 履歴 ----
    def _detail_rows(self, start: datetime, end: datetime, email: str | None, filters: dict | None = None,
                     limit: int | None = None, offset: int = 0) -> list[dict]:
        hours = self._hours(start, end)
        hour_set = set(hours)
        days = sorted({date.fromisoformat(dt) for dt, _ in hours})
        # 締まった日は日ごとのファイル（1 日 1 個）、まだの日は時間ごとのファイル、まだ整形していない時間は原本を読む
        per_day = self._derived_keys("meta_day", days)
        parquet = self._derived_keys("meta", [d for d in days if d.isoformat() not in per_day])
        have, files = set(), []
        for day, keys in per_day.items():
            files += [self.url(k) for k in keys]
            have |= {h for h in hour_set if h[0] == day}
        for keys in parquet.values():
            for k in keys:
                parts = k.split("/")
                dt, hour = parts[-3].removeprefix("dt="), parts[-2].removeprefix("hour=")
                if (dt, hour) in hour_set:
                    have.add((dt, hour))
                    files.append(self.url(k))
        cond, params = ["ts_start >= ?", "ts_start <= ?"], [start, end]
        if email:
            cond.append("user_email = ?")
            params.append(email)
        for col in ("model_group", "client", "status"):
            if (filters or {}).get(col):
                cond.append(f"{col} = ?")
                params.append(filters[col])
        if (filters or {}).get("blocked"):
            cond.append("review_blocked")
        if (filters or {}).get("tool"):
            cond.append("tool_names LIKE ?")
            params.append(f"%{filters['tool']}%")
        rows: list[dict] = []
        if files:
            lim = f"LIMIT {int(limit) + int(offset)}" if limit is not None else ""
            # 文脈独立審査の列を足す前の Parquet と混ざるので、列の名前で合わせて読む
            cur = self.con().execute(f"SELECT * FROM read_parquet({files!r}, union_by_name=true) "
                                     f"WHERE {' AND '.join(cond)} "
                                     f"ORDER BY ts_start DESC {lim}", params)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
            for r in rows:
                r["ts_start"] = r["ts_start"].isoformat() if r.get("ts_start") else None
                r["ts_end"] = r["ts_end"].isoformat() if r.get("ts_end") else None
        for r in self._raw_rows(hours, have):
            ts = parse_iso(r["ts_start"]) if r.get("ts_start") else None
            if ts is None or not (start <= ts <= end):
                continue
            if email and r.get("user_email") != email:
                continue
            if any((filters or {}).get(c) and r.get(c) != filters[c] for c in ("model_group", "client", "status")):
                continue
            if (filters or {}).get("blocked") and not r.get("review_blocked"):
                continue
            if (filters or {}).get("tool") and filters["tool"] not in (r.get("tool_names") or ""):
                continue
            rows.append(r)
        rows.sort(key=lambda r: r.get("ts_start") or "", reverse=True)
        if limit is not None:
            rows = rows[offset:offset + limit]
        return rows

    def requests(self, start: datetime, end: datetime, email: str | None, filters: dict, page: int,
                 per_page: int = 100) -> tuple[list[dict], bool]:
        rows = self._detail_rows(start, end, email, filters, limit=per_page + 1, offset=page * per_page)
        return rows[:per_page], len(rows) > per_page

    def request(self, call_id: str, at: datetime, email: str | None) -> dict | None:
        """call_id の 1 件（at は記録の時刻。前後 2 時間を探す）。"""
        for r in self._detail_rows(at - timedelta(hours=2), at + timedelta(hours=2), email):
            if r.get("call_id") == call_id:
                return r
        return None

    def body(self, row: dict) -> dict:
        return read_body(self.s3, {"key": row["body_key"], "offset": row["body_offset"],
                                   "length": row["body_length"]})

    # ---- 閲覧の記録と管理操作の記録（audit-access。原本と同じ 90 日、消せない形） ----
    def _access_key(self, kind: str) -> str:
        t = now_jst()
        stamp = t.strftime("%Y%m%dT%H%M%S") + f"{t.microsecond:06d}JST"
        hour = f"hour={t.strftime('%H')}/" if kind == "view" else ""
        return f"{kind}/dt={t.strftime('%Y-%m-%d')}/{hour}{self.pod}-{stamp}.ndjson.gz"

    def write_access(self, kind: str, record: dict) -> None:
        body = (json.dumps({"schema": 1, "type": kind, "ts": now_jst().isoformat(timespec="milliseconds"),
                            **record}, ensure_ascii=False, default=str) + "\n").encode()
        self.s3.put_object(**ACCESS.ref(self._access_key(kind)), Body=gzip.compress(body, mtime=0),
                           **retention_args("AUDIT_ACCESS_RETENTION_DAYS"))

    def read_access(self, kind: str, start: date, end: date) -> list[dict]:
        rows: list[dict] = []
        for i in range((end - start).days + 1):
            d = start + timedelta(days=i)
            for o in list_keys(self.s3, ACCESS, f"{kind}/dt={d.isoformat()}/"):
                rows += read_ndjson_gz(self.s3, ACCESS, o["Key"])
        return sorted(rows, key=lambda r: r.get("ts") or "", reverse=True)

    # ---- 精算、請求の単位、レート ----
    def settlement(self, month: str, email: str | None) -> list[dict]:
        keys = [o["Key"] for o in list_keys(self.s3, DERIVED, f"{self.prefix}billing/settlement/month={month}/")]
        if not keys:
            return []
        cur = self.con().execute(f"SELECT * FROM read_parquet({[self.url(k) for k in keys]!r})")
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        if email:
            rows = [r for r in rows if r.get("user_email") == email]
        return rows

    def contracts(self) -> tuple[str | None, dict]:
        version, doc = latest_json(self.s3, DERIVED, "billing/contracts/")
        return version, doc or {"schema": 1, "contracts": []}

    def fx_rates(self) -> tuple[str | None, dict]:
        version, doc = latest_json(self.s3, DERIVED, "billing/fx_rates/")
        return version, doc or {"schema": 1, "usd_jpy": {}}

    def save_versioned(self, kind: str, doc: dict) -> str:
        """編集のたびに新しいファイルを足す（上書きしない。2 台が同時に書いても片方が消えない）。"""
        t = now_jst()
        key = f"billing/{kind}/{t.strftime('%Y%m%dT%H%M%S')}{t.microsecond:06d}-{self.pod}.json"
        put_json(self.s3, DERIVED, key, doc)
        return key

    def previous_version(self, kind: str, key: str) -> str | None:
        keys = sorted(o["Key"] for o in list_keys(self.s3, DERIVED, f"billing/{kind}/"))
        i = keys.index(key) if key in keys else -1
        return keys[i - 1] if i > 0 else None

    def compactor_state(self) -> dict:
        try:
            return json.loads(self.s3.get_object(**DERIVED.ref(f"{self.prefix}_state/compactor.json"))["Body"]
                              .read())
        except Exception:  # noqa: BLE001 まだ一度も動いていない
            return {}
