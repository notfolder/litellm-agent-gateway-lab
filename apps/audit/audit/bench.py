"""検索の速さの試験（変更仕様書 7.4・9.4 節）。検索アプリと同じ関数（Store）で測る。

python -m audit.bench [--prefix synth/]  … 合成データ（または実際のデータ）で、目標の 4 つの操作の時間を測る。
本文の表示は、合成データには本文が無いので、実際の記録（--prefix なし）で測る。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

from .common import JST, Config, list_keys, now_jst
from .data import Store

TARGETS = {"usage_month_all": 3.0, "history_user_day": 3.0, "history_user_90d": 30.0, "body": 1.0}


def timed(fn):
    t = time.monotonic()
    out = fn()
    return round(time.monotonic() - t, 2), out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m audit.bench")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--user", default="user1@example.com")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    cfg.data_prefix = args.prefix
    store = Store(cfg, pod="bench")
    days = sorted({o["Key"].split("/")[-2].removeprefix("dt=")
                   for o in list_keys(store.s3, "audit-derived", f"{args.prefix}usage_daily/")})
    results: dict = {"prefix": args.prefix, "days_with_data": len(days)}
    if days:
        last = datetime.fromisoformat(days[-1]).date()
        first = max(datetime.fromisoformat(days[0]).date(), last - timedelta(days=29))
        results["usage_month_all"], rows = timed(lambda: store.usage(first, last, None, ["user_email"]))
        results["usage_month_all_rows"] = len(rows)
        d0 = datetime(last.year, last.month, last.day, tzinfo=JST)
        results["history_user_day"], r1 = timed(lambda: store.requests(d0, d0 + timedelta(hours=23, minutes=59),
                                                                      args.user, {}, 0))
        start90 = d0 - timedelta(days=89)
        results["history_user_90d"], r2 = timed(lambda: store.requests(start90, d0 + timedelta(hours=23, minutes=59),
                                                                       args.user, {}, 0))
        results["history_rows"] = [len(r1[0]), len(r2[0])]
    if not args.prefix:
        now = now_jst()
        rows, _ = store.requests(now - timedelta(days=1), now, None, {}, 0)
        row = next((r for r in rows if r.get("body_key")), None)
        if row:
            results["body"], _ = timed(lambda: store.body(row))
    results["targets"] = TARGETS
    results["passed"] = all(results[k] <= v for k, v in TARGETS.items() if k in results)
    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
