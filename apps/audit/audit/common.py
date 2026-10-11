"""監査ログの整形と検索の共通の部品: 設定、S3、日本時間、請求の単位、精算の計算（変更設計書 5〜7 章）。"""

from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))
RAW, ACCESS, DERIVED = "audit-raw", "audit-access", "audit-derived"


# ---- 設定 ----
@dataclass
class Config:
    endpoint: str
    region: str
    access_key: str
    secret_key: str
    data_prefix: str = ""          # 合成データを読むときは "synth/"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(endpoint=os.environ.get("AUDIT_S3_ENDPOINT", "http://rustfs:9000"),
                   region=os.environ.get("AUDIT_S3_REGION", "us-east-1"),
                   access_key=os.environ.get("AUDIT_S3_ACCESS_KEY", ""),
                   secret_key=os.environ.get("AUDIT_S3_SECRET_KEY", ""),
                   data_prefix=os.environ.get("AUDIT_DATA_PREFIX", ""))

    def s3(self):
        import boto3
        from botocore.config import Config as BotoConfig
        return boto3.client("s3", endpoint_url=self.endpoint, region_name=self.region,
                            aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
                            config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}))

    def duckdb_secret_sql(self) -> str:
        """DuckDB の S3 の接続（接続先などは設定から組み立てる。変更設計書 6.1 節）。"""
        u = urlparse(self.endpoint)
        host = u.netloc or u.path
        ssl = "true" if u.scheme == "https" else "false"
        return (f"CREATE OR REPLACE SECRET lab_s3 (TYPE s3, KEY_ID '{self.access_key}', SECRET '{self.secret_key}', "
                f"ENDPOINT '{host}', URL_STYLE 'path', USE_SSL {ssl}, REGION '{self.region}')")


def duckdb_connect(cfg: Config, memory_limit: str = "384MB"):
    import duckdb
    con = duckdb.connect()
    con.execute("LOAD httpfs")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(cfg.duckdb_secret_sql())
    return con


# ---- 日本時間 ----
def now_jst() -> datetime:
    return datetime.now(JST)


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def hour_key(ts: str | datetime) -> tuple[str, str]:
    """記録の時刻（UTC の ISO 8601）から、日本時間の日と時。"""
    t = parse_iso(ts) if isinstance(ts, str) else ts
    t = t.astimezone(JST)
    return t.strftime("%Y-%m-%d"), t.strftime("%H")


def next_hour(dt: str, hour: str) -> tuple[str, str]:
    t = datetime.strptime(f"{dt} {hour}", "%Y-%m-%d %H").replace(tzinfo=JST) + timedelta(hours=1)
    return t.strftime("%Y-%m-%d"), t.strftime("%H")


def hour_start(dt: str, hour: str) -> datetime:
    return datetime.strptime(f"{dt} {hour}", "%Y-%m-%d %H").replace(tzinfo=JST)


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def month_of(d: date | str) -> str:
    return (d if isinstance(d, str) else d.isoformat())[:7]


def month_days(month: str) -> list[date]:
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return days_between(first, nxt - timedelta(days=1))


# ---- S3 の読み書き ----
def list_keys(s3, bucket: str, prefix: str) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        out += res.get("Contents", [])
        if not res.get("IsTruncated"):
            return out
        token = res.get("NextContinuationToken")


def read_ndjson_gz(s3, bucket: str, key: str) -> list[dict]:
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return [json.loads(line) for line in gzip.decompress(data).decode().splitlines() if line.strip()]


def raw_meta_prefix(dt: str, hour: str | None = None) -> str:
    return f"raw/meta/dt={dt}/" + (f"hour={hour}/" if hour else "")


def read_raw_hour(s3, dt: str, hour: str) -> list[dict]:
    rows: list[dict] = []
    for obj in list_keys(s3, RAW, raw_meta_prefix(dt, hour)):
        rows += read_ndjson_gz(s3, RAW, obj["Key"])
    return rows


def read_body(s3, body: dict) -> dict:
    """記録の body（{key, offset, length}）から、本文 1 件だけを範囲指定で読む。"""
    start, length = int(body["offset"]), int(body["length"])
    data = s3.get_object(Bucket=RAW, Key=body["key"], Range=f"bytes={start}-{start + length - 1}")["Body"].read()
    return json.loads(gzip.decompress(data))


def latest_json(s3, bucket: str, prefix: str) -> tuple[str | None, dict | None]:
    """接頭辞の下で名前が最も新しいファイル（名前は時刻で始まる）。版の名前と中身を返す。"""
    keys = sorted(o["Key"] for o in list_keys(s3, bucket, prefix))
    if not keys:
        return None, None
    return keys[-1], json.loads(s3.get_object(Bucket=bucket, Key=keys[-1])["Body"].read())


def put_json(s3, bucket: str, key: str, obj) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(obj, ensure_ascii=False, indent=2).encode(),
                  ContentType="application/json")


def put_ndjson_gz(s3, bucket: str, key: str, rows: list[dict]) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows).encode()
    s3.put_object(Bucket=bucket, Key=key, Body=gzip.compress(body, mtime=0))


# ---- 請求の単位 ----
def model_matches(pattern: str, *names: str | None) -> bool:
    """完全一致か、末尾の * による前方一致。model_group と model のどちらかに当たればよい（変更設計書 7.3 節）。"""
    for name in names:
        if not name:
            continue
        if pattern.endswith("*") and name.startswith(pattern[:-1]):
            return True
        if pattern == name:
            return True
    return False


def contract_for(contracts: list[dict], model_group: str | None, model: str | None) -> str | None:
    for c in contracts:
        if any(model_matches(p, model_group, model) for p in c.get("models") or []):
            return c["id"]
    return None


def validate_contracts(doc: dict) -> list[str]:
    """請求の単位の定義の誤り（空なら正しい）。1 つのモデル名が 2 つの単位に当たる指定は断る。"""
    errors: list[str] = []
    contracts = doc.get("contracts") or []
    ids = [c.get("id") for c in contracts]
    if len(set(ids)) != len(ids) or not all(ids):
        errors.append("id が空か、重複しています")
    for c in contracts:
        if c.get("kind") not in ("subscription", "invoice"):
            errors.append(f"{c.get('id')}: kind は subscription か invoice です")
        if c.get("currency") not in ("USD", "JPY"):
            errors.append(f"{c.get('id')}: currency は USD か JPY です")
        for m, v in (c.get("fees") or {}).items():
            if not re.fullmatch(r"\d{4}-\d{2}", m) or not isinstance(v, (int, float)) or v < 0:
                errors.append(f"{c.get('id')}: fees の {m} が不正です")
    pats = [(c.get("id"), p) for c in contracts for p in (c.get("models") or [])]
    for i, (ci, pi) in enumerate(pats):
        for cj, pj in pats[i + 1:]:
            if ci == cj:
                continue
            a, b = pi.rstrip("*"), pj.rstrip("*")
            overlap = (pi == pj or (pi.endswith("*") and pj.startswith(a)) or (pj.endswith("*") and pi.startswith(b)))
            if overlap:
                errors.append(f"モデル {pi}（{ci}）と {pj}（{cj}）が重なっています")
    return errors


# ---- 精算（変更仕様書 6.7 節、変更設計書 7.2 節） ----
MINOR = {"USD": Decimal("0.01"), "JPY": Decimal("1")}


def _allocate(total: Decimal, weights: dict[str, Decimal], unit: Decimal) -> dict[str, Decimal]:
    """total を weights の割合で配る。各自の端数を切り捨て、残りを重みが最大の人に足す（合計がちょうど合う）。"""
    s = sum(weights.values())
    if s == 0:
        return {}
    out = {k: (total * w / s).quantize(unit, rounding=ROUND_DOWN) for k, w in weights.items()}
    rest = total - sum(out.values())
    if rest:
        top = max(weights, key=lambda k: (weights[k], k))
        out[top] += rest
    return out


def settle(month: str, usage: list[dict], contracts: list[dict], fx_usd_jpy: float | None, *, closed: bool,
           contracts_version: str | None = None, fx_version: str | None = None, computed_at: str = "") -> list[dict]:
    """1 か月の精算。usage は日次の集計の行（user_id, user_email, model_group, model, category, cost_usd）。

    A（概算）= 利用者ごとの cost_usd。B（按分）= 実費 F を A の割合で配る。F の通貨で計算し、最後に円にする。
    利用者なし（user_id が空）も 1 人として配る。どの単位にも当たらないモデルは「対象外」に A だけを出す。"""
    rows: list[dict] = []
    by_contract: dict[str | None, dict[str, dict]] = {}
    for u in usage:
        cid = contract_for(contracts, u.get("model_group"), u.get("model"))
        who = u.get("user_id") or ""
        acc = by_contract.setdefault(cid, {}).setdefault(who, {"user_email": u.get("user_email"),
                                                               "request": Decimal(0), "review": Decimal(0)})
        acc["request" if u.get("category") != "review" else "review"] += Decimal(str(u.get("cost_usd") or 0))
        acc["user_email"] = acc["user_email"] or u.get("user_email")
    fx = Decimal(str(fx_usd_jpy)) if fx_usd_jpy else None

    def base(kind: str, cid: str | None, who: str, acc: dict | None) -> dict:
        a_req = acc["request"] if acc else Decimal(0)
        a_rev = acc["review"] if acc else Decimal(0)
        return {"month": month, "row_kind": kind, "contract_id": cid,
                "user_id": who if kind == "user" else None,
                "user_email": acc["user_email"] if acc and kind == "user" else None,
                "cost_a_request": float(a_req), "cost_a_review": float(a_rev),
                "cost_a_jpy": float((a_req + a_rev) * fx) if fx is not None else None,
                "cost_b": None, "cost_b_jpy": None, "rate": None, "fee": None, "currency": None,
                "fx_rate": float(fx) if fx is not None else None, "fx_status": None, "status": None,
                "contracts_version": contracts_version, "fx_version": fx_version, "computed_at": computed_at}

    for c in contracts:
        cid, cur = c["id"], c["currency"]
        users = by_contract.get(cid, {})
        fee_raw = (c.get("fees") or {}).get(month)
        fee = Decimal(str(fee_raw)) if fee_raw is not None else None
        weights = {who: acc["request"] + acc["review"] for who, acc in users.items()}
        total_a = sum(weights.values(), Decimal(0))
        if fee is None:
            status = "no_fee"
        elif closed:
            status = "final"
        else:
            status = "estimate" if c.get("kind") == "subscription" else "no_fee"
        if status == "no_fee":
            fee_used = None
        else:
            fee_used = fee
        alloc = _allocate(fee_used, weights, MINOR[cur]) if fee_used is not None and total_a > 0 else {}
        if cur == "JPY":
            fx_status, jpy_alloc = "not_needed", {k: v for k, v in alloc.items()}
        elif fx is None:
            fx_status, jpy_alloc = "missing", {}
        else:
            fx_status = "ok"
            jpy_alloc = _allocate((fee_used * fx).quantize(Decimal(1), rounding=ROUND_DOWN), weights, Decimal(1)) \
                if fee_used is not None and total_a > 0 else {}
        rate = (fee_used / total_a) if fee_used is not None and total_a > 0 else None
        for who, acc in sorted(users.items()):
            r = base("user" if who else "no_user", cid, who, acc)
            r |= {"fee": float(fee) if fee is not None else None, "currency": cur, "status": status,
                  "fx_status": fx_status, "rate": float(rate) if rate is not None else None,
                  "cost_b": float(alloc[who]) if who in alloc else None,
                  "cost_b_jpy": float(jpy_alloc[who]) if who in jpy_alloc else None}
            if cur == "JPY":
                r["cost_a_jpy"] = None   # A は米ドル。円の単位では換算しない
            rows.append(r)
        if fee_used is not None and total_a == 0:
            r = base("unallocated", cid, "", None)
            r |= {"fee": float(fee_used), "currency": cur, "status": status, "fx_status": fx_status,
                  "cost_b": float(fee_used),
                  "cost_b_jpy": (float(fee_used) if cur == "JPY" else
                                 float((fee_used * fx).quantize(Decimal(1), rounding=ROUND_DOWN)) if fx else None)}
            rows.append(r)
    for who, acc in sorted(by_contract.get(None, {}).items()):
        rows.append(base("uncontracted", None, who, acc) | {"user_id": who or None, "user_email": acc["user_email"]})
    return rows
