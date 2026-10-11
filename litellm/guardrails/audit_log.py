"""監査ログの書き込み（変更仕様書 6 章、変更設計書 5 章）。

LiteLLM のコールバック（CustomLogger）。要求ごとの記録（小さな JSON 1 行）と本文（LiteLLM の
StandardLoggingPayload をそのまま）を Pod の中に溜め、一定の間隔で S3 互換ストレージ（audit-raw）に書く。

- 置き場所: raw/meta/dt=<日本時間の日>/hour=<時>/<pod>-<時刻>-<連番>.ndjson.gz と、同じ名前の raw/body/….gz
- 本文のファイルは、1 件ごとに独立した gzip を順につないだもの。記録の body に {key, offset, length} を書くので、
  範囲指定の取得で 1 件だけ読める。ファイル全体も通常の gzip として展開できる
- 要求と審査は litellm_call_id（記録の call_id）で結び付ける
- 審査のガードレールの中の呼び出し（審査官と Jev）は記録しない（利用量は審査の記録に書く）
- S3 に書けないときはローカルのディスクに退避して 30 秒ごとに再送する。退避が上限を超えたら捨てて数える
- プロセスが止まるとき（処理中の要求を待ち終えた後）に、残りを書き出す
"""

from __future__ import annotations

import atexit
import gzip
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # 単体テストを litellm 無しで流すときの代わり
    class CustomLogger:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

from clientkind import client_kind

SCHEMA = 1
LOG_PREFIX = "audit_log"
INTERNAL_MARK = "tool_call_review_internal"   # 審査のガードレールの中の呼び出しに付ける印
JST = timezone(timedelta(hours=9))


# ---- 記録の組み立て（副作用なし。単体テストの対象） ----
def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def partition(ts: float) -> tuple[str, str]:
    """日本時間の日と時（置き場所の dt と hour）。"""
    t = datetime.fromtimestamp(float(ts), tz=JST)
    return t.strftime("%Y-%m-%d"), t.strftime("%H")


def gzip_member(data: bytes) -> bytes:
    """独立した 1 つの gzip。つないでも全体を gzip として展開できる。"""
    return gzip.compress(data, mtime=0)


def _headers(kwargs: dict) -> dict:
    lp = kwargs.get("litellm_params") or {}
    req = lp.get("proxy_server_request") or {}
    return {str(k).lower(): v for k, v in (req.get("headers") or {}).items()}


def is_internal(kwargs: dict, slp: dict) -> bool:
    """審査のガードレールの中の呼び出し（審査官、Jev）か。"""
    lp = kwargs.get("litellm_params") or {}
    md = lp.get("metadata") or {}
    requester = ((slp.get("metadata") or {}).get("requester_metadata") or {})
    if md.get(INTERNAL_MARK) or requester.get(INTERNAL_MARK):
        return True
    call_type = str(slp.get("call_type") or kwargs.get("call_type") or "")
    return "decision" in call_type   # Jev（litellm.adecisions）は no-log が効かないので種類で外す


def _tool_calls(response: Any) -> list[dict]:
    """応答の中のツールコールの名前と、引数の先頭 200 文字。"""
    out: list[dict] = []
    if not isinstance(response, dict):
        return out
    for choice in response.get("choices") or []:
        msg = (choice or {}).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            out.append({"name": fn.get("name"), "args_head": str(fn.get("arguments") or "")[:200]})
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            args = item.get("arguments") or item.get("input") or item.get("action") or ""
            out.append({"name": item.get("name") or item.get("type"),
                        "args_head": (args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))[:200]})
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append({"name": block.get("name"), "args_head": json.dumps(block.get("input"), ensure_ascii=False)[:200]})
    return out


def build_request_record(kwargs: dict, slp: dict, pod: str) -> dict:
    """要求 1 件の記録（本文の場所 body は、書き出すときに足す）。"""
    md = slp.get("metadata") or {}
    usage = md.get("usage_object") or {}
    ptd = usage.get("prompt_tokens_details") or {}
    hidden = slp.get("hidden_params") or {}
    call_type = str(slp.get("call_type") or "")
    mcp = md.get("mcp_tool_call_metadata") or {}
    headers = _headers(kwargs)
    start, end, first = slp.get("startTime"), slp.get("endTime"), slp.get("completionStartTime")
    record = {
        "schema": SCHEMA,
        "type": "mcp" if call_type == "call_mcp_tool" else "request",
        "call_id": kwargs.get("litellm_call_id") or slp.get("id"),
        "ts_start": _iso(start),
        "ts_end": _iso(end),
        "pod": pod,
        "response_id": slp.get("id"),
        # マスターキーの要求は、LiteLLM が仮の利用者 default_user_id を付ける。利用者なしとして扱う
        "user_id": (md.get("user_api_key_user_id") or None)
        if md.get("user_api_key_user_id") != "default_user_id" else None,
        "user_email": md.get("user_api_key_user_email") or None,
        "team_id": md.get("user_api_key_team_id") or None,
        "team_alias": md.get("user_api_key_team_alias") or None,
        "key_alias": md.get("user_api_key_alias") or None,
        "key_hash": md.get("user_api_key_hash") or None,
        "end_user": slp.get("end_user") or None,
        "client": client_kind(headers.get("user-agent") or ""),
        "session_id": slp.get("session_id") or headers.get("session-id") or headers.get("thread-id"),
        "trace_id": slp.get("trace_id"),
        "call_type": call_type,
        "model_group": slp.get("model_group") or None,
        "model": slp.get("model") or None,
        "model_id": slp.get("model_id") or None,
        "api_base": slp.get("api_base") or None,
        "status": slp.get("status"),
        "error_class": (slp.get("error_information") or {}).get("error_class") or None,
        "tokens_in": int(slp.get("prompt_tokens") or 0),
        "tokens_out": int(slp.get("completion_tokens") or 0),
        "tokens_cache_read": int(ptd.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0),
        "tokens_cache_write": int(ptd.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(slp.get("response_cost") or 0.0),
        "price_source": "public_equivalent" if hidden.get("custom_llm_provider") == "chatgpt" else "litellm",
        "latency_ms": int((end - start) * 1000) if start and end else None,
        "ttft_ms": int((first - start) * 1000) if start and first else None,
        "tool_calls": _tool_calls(slp.get("response")),
    }
    if record["type"] == "mcp":
        args = mcp.get("arguments")
        record |= {"mcp_server": mcp.get("mcp_server_name"), "mcp_tool": mcp.get("name"),
                   "mcp_arguments_head": (args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))[:200]}
    return record


def build_body(record: dict, slp: dict) -> bytes:
    return json.dumps({"schema": SCHEMA, "call_id": record["call_id"], "payload": slp},
                      ensure_ascii=False, default=str).encode()


# ---- 溜める ----
@dataclass
class Segment:
    """1 回の書き出しで 1 つの時間（dt, hour）に書く、記録と本文の組。"""

    dt: str
    hour: str
    base: str
    meta: list[bytes] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)

    @property
    def body_key(self) -> str:
        return f"raw/body/dt={self.dt}/hour={self.hour}/{self.base}.gz"

    @property
    def meta_key(self) -> str:
        return f"raw/meta/dt={self.dt}/hour={self.hour}/{self.base}.ndjson.gz"

    def add(self, record: dict, body: bytes | None) -> None:
        if body is not None:
            member = gzip_member(body)
            record["body"] = {"key": self.body_key, "offset": len(self.body), "length": len(member)}
            record["body_bytes"] = len(body)
            self.body += member
        self.meta.append(json.dumps(record, ensure_ascii=False, default=str).encode() + b"\n")

    def files(self) -> list[tuple[str, bytes]]:
        """書く順（本文が先。記録が指す本文が必ずある）。"""
        out = []
        if self.body:
            out.append((self.body_key, bytes(self.body)))
        out.append((self.meta_key, gzip.compress(b"".join(self.meta), mtime=0)))
        return out


class Buffer:
    """時間ごとの Segment を溜める。名前は Pod 名、書き出しの開始時刻（日本時間）、連番で一意。"""

    def __init__(self, pod: str) -> None:
        self.pod = pod
        self.seq = 0
        self.segments: dict[tuple[str, str], Segment] = {}
        self.records = 0

    def add(self, ts: float, record: dict, body: bytes | None) -> None:
        dt, hour = partition(ts)
        seg = self.segments.get((dt, hour))
        if seg is None:
            self.seq += 1
            stamp = datetime.now(JST).strftime("%Y%m%dT%H%M%S") + "JST"
            seg = self.segments[(dt, hour)] = Segment(dt, hour, f"{self.pod}-{stamp}-{self.seq:06d}")
        seg.add(record, body)
        self.records += 1

    @property
    def body_bytes(self) -> int:
        return sum(len(s.body) for s in self.segments.values())

    def take(self) -> tuple[list[Segment], int]:
        segs, n = list(self.segments.values()), self.records
        self.segments, self.records = {}, 0
        return segs, n


# ---- 書き出し ----
class AuditLogger(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.endpoint = os.environ.get("AUDIT_S3_ENDPOINT", "")
        self.region = os.environ.get("AUDIT_S3_REGION", "us-east-1")
        self.bucket = os.environ.get("AUDIT_S3_BUCKET", "audit-raw")
        self.access_key = os.environ.get("AUDIT_S3_ACCESS_KEY", "")
        self.secret_key = os.environ.get("AUDIT_S3_SECRET_KEY", "")
        self.flush_seconds = float(os.environ.get("AUDIT_FLUSH_SECONDS", "60"))
        self.max_body = int(os.environ.get("AUDIT_MAX_BODY_BYTES", str(64 * 1024 * 1024)))
        self.spool = Path(os.environ.get("AUDIT_SPOOL_DIR", "/var/lib/audit-spool"))
        self.spool_limit = int(os.environ.get("AUDIT_SPOOL_LIMIT_BYTES", str(900 * 1024 * 1024)))
        self.pod = os.environ.get("HOSTNAME") or socket.gethostname()
        self.enabled = bool(self.endpoint and self.access_key and self.secret_key)
        self.buffer = Buffer(self.pod)
        self.lock = threading.Lock()
        self.upload_lock = threading.Lock()
        self.stats = {"written": 0, "spooled_bytes": 0, "dropped": 0, "upload_errors": 0}
        self._client = None
        self._stop = threading.Event()
        self._last_flush = time.monotonic()
        if not self.enabled:
            print(f"{LOG_PREFIX}: S3 の設定が無いので監査ログは書きません", flush=True)
            return
        self.spool.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._loop, name="audit-log-flush", daemon=True).start()
        atexit.register(self.close)
        print(f"{LOG_PREFIX}: init endpoint={self.endpoint} bucket={self.bucket} flush={self.flush_seconds}s "
              f"pod={self.pod}", flush=True)

    # ---- LiteLLM のフック ----
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs)

    def _record(self, kwargs: dict) -> None:
        if not self.enabled:
            return
        try:
            slp = kwargs.get("standard_logging_object") or {}
            if not slp or is_internal(kwargs, slp):
                return
            record = build_request_record(kwargs, slp, self.pod)
            body = build_body(record, slp)
            with self.lock:
                self.buffer.add(float(slp.get("startTime") or time.time()), record, body)
        except Exception as e:  # noqa: BLE001 監査ログの失敗で要求を止めない
            import traceback
            where = traceback.extract_tb(e.__traceback__)[-1]
            print(f"{LOG_PREFIX}: 記録を作れませんでした: {type(e).__name__}: {e}（{where.name}:{where.lineno}）",
                  flush=True)

    def record_review(self, entry: dict) -> None:
        """審査のガードレールから、判定 1 件を受け取る（記録だけ。本文は無い）。"""
        if not self.enabled:
            return
        record = {"schema": SCHEMA, "type": "review", "call_id": None, "pod": self.pod,
                  "ts_start": _iso(time.time()), **entry}
        with self.lock:
            self.buffer.add(time.time(), record, None)

    # ---- 書き出し ----
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client("s3", endpoint_url=self.endpoint, region_name=self.region,
                                        aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
                                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2},
                                                      connect_timeout=5, read_timeout=30))
        return self._client

    def _put(self, key: str, data: bytes) -> None:
        self.client().put_object(Bucket=self.bucket, Key=key, Body=data)

    def _spool_size(self) -> int:
        return sum(p.stat().st_size for p in self.spool.glob("*") if p.is_file())

    def _spool_segment(self, seg: Segment, records: int) -> None:
        files = seg.files()
        size = sum(len(d) for _, d in files)
        if self._spool_size() + size > self.spool_limit:
            self.stats["dropped"] += records
            print(f"{LOG_PREFIX}: 退避が上限を超えたので {records} 件を捨てました（累計 {self.stats['dropped']} 件）",
                  flush=True)
            return
        stamp = f"{time.time_ns()}-{seg.base}"
        (self.spool / f"{stamp}.json").write_text(json.dumps({"keys": [k for k, _ in files], "records": records}))
        for i, (_, data) in enumerate(files):
            (self.spool / f"{stamp}.{i}").write_bytes(data)

    def _resend_spool(self) -> None:
        for manifest in sorted(self.spool.glob("*.json")):
            stamp = manifest.name[:-5]
            info = json.loads(manifest.read_text())
            try:
                for i, key in enumerate(info["keys"]):
                    self._put(key, (self.spool / f"{stamp}.{i}").read_bytes())
            except Exception:  # noqa: BLE001 まだ書けない。次の機会に送る
                self.stats["upload_errors"] += 1
                return
            self.stats["written"] += info.get("records", 0)
            for i in range(len(info["keys"])):
                (self.spool / f"{stamp}.{i}").unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)

    def flush(self) -> None:
        with self.lock:
            segs, records = self.buffer.take()
        if not segs:
            return
        with self.upload_lock:
            per_seg = max(1, records // len(segs))
            for seg in segs:
                n = len(seg.meta)
                try:
                    for key, data in seg.files():
                        self._put(key, data)
                    self.stats["written"] += n
                except Exception as e:  # noqa: BLE001 S3 に書けない。退避する
                    self.stats["upload_errors"] += 1
                    print(f"{LOG_PREFIX}: S3 に書けないので退避します: {type(e).__name__}: {str(e)[:200]}", flush=True)
                    self._spool_segment(seg, n or per_seg)
        self._last_flush = time.monotonic()

    def _loop(self) -> None:
        last_stats = last_resend = time.monotonic()
        while not self._stop.is_set():
            time.sleep(1)
            now = time.monotonic()
            with self.lock:
                full = self.buffer.body_bytes >= self.max_body
            if full or now - self._last_flush >= self.flush_seconds:
                self.flush()
            if now - last_resend >= 30:
                with self.upload_lock:
                    self._resend_spool()
                last_resend = now
            if now - last_stats >= 60:
                self.stats["spooled_bytes"] = self._spool_size()
                print(f"{LOG_PREFIX} " + json.dumps({"event": "stats", "pod": self.pod, **self.stats}), flush=True)
                last_stats = now

    def close(self) -> None:
        """止まるとき（処理中の要求を待ち終えた後）に残りを書き出す。書けなかった分は件数を警告に出す。"""
        self._stop.set()
        self.flush()
        with self.upload_lock:
            self._resend_spool()
        lost = sum(json.loads(m.read_text()).get("records", 0) for m in self.spool.glob("*.json"))
        if lost:
            print(f"{LOG_PREFIX}: 警告: S3 に書けないまま止まるので、退避していた {lost} 件の記録が失われます"
                  f"（audit_log_lost_on_shutdown）", flush=True)


audit_logger_instance = AuditLogger()


def record_review(entry: dict) -> None:
    audit_logger_instance.record_review(entry)
