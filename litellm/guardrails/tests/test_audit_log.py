"""監査ログの書き込み（audit_log.py）の単体テスト。S3 には触らない。"""

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit_log as al  # noqa: E402


def slp(**extra):
    base = {
        "id": "chatcmpl-up", "call_type": "acompletion", "startTime": 1760194800.0, "endTime": 1760194801.5,
        "completionStartTime": 1760194800.25, "model_group": "fake-tools", "model": "openai/fake-tools",
        "status": "success", "prompt_tokens": 10, "completion_tokens": 5, "response_cost": 0.0012,
        "metadata": {"user_api_key_user_id": "u1@example.com", "user_api_key_user_email": "u1@example.com",
                     "user_api_key_alias": "user1-codex", "user_api_key_hash": "h",
                     "usage_object": {"prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2}}},
        "messages": [{"role": "user", "content": "hi"}],
        "response": {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "shell", "arguments": '{"command":["ls"]}'}}]}}]},
        "hidden_params": {},
    }
    base.update(extra)
    return base


KW = {"litellm_call_id": "call-1", "litellm_params": {"proxy_server_request": {
    "headers": {"User-Agent": "codex_cli_rs/0.162.1"}}}}


class RecordTest(unittest.TestCase):
    def test_request_record(self):
        r = al.build_request_record(KW, slp(), "pod-a")
        self.assertEqual(r["call_id"], "call-1")          # 結び付けは litellm_call_id
        self.assertEqual(r["response_id"], "chatcmpl-up")  # 上流の応答の ID は別の項目
        self.assertEqual(r["type"], "request")
        self.assertEqual(r["client"], "codex")
        self.assertEqual((r["tokens_in"], r["tokens_out"], r["tokens_cache_read"], r["tokens_cache_write"]),
                         (10, 5, 3, 2))
        self.assertEqual(r["latency_ms"], 1500)
        self.assertEqual(r["ttft_ms"], 250)
        self.assertEqual(r["tool_calls"], [{"name": "shell", "args_head": '{"command":["ls"]}'}])
        self.assertEqual(r["ts_start"], "2025-10-11T15:00:00.000Z")
        self.assertNotIn("api_key", json.dumps(r))

    def test_responses_and_anthropic_tool_calls(self):
        r = al.build_request_record(KW, slp(response={"output": [
            {"type": "function_call", "name": "exec", "arguments": "x" * 300}]}), "p")
        self.assertEqual(r["tool_calls"][0]["name"], "exec")
        self.assertEqual(len(r["tool_calls"][0]["args_head"]), 200)
        r = al.build_request_record(KW, slp(response={"content": [{"type": "tool_use", "name": "Bash",
                                                                    "input": {"command": "ls"}}]}), "p")
        self.assertEqual(r["tool_calls"][0]["name"], "Bash")

    def test_mcp_record(self):
        s = slp(call_type="call_mcp_tool", model="MCP: gmail_list_labels")
        s["metadata"]["mcp_tool_call_metadata"] = {"mcp_server_name": "gmail", "name": "list_labels",
                                                   "arguments": {"user": "x"}}
        r = al.build_request_record(KW, s, "p")
        self.assertEqual((r["type"], r["mcp_server"], r["mcp_tool"]), ("mcp", "gmail", "list_labels"))
        self.assertIn('"user"', r["mcp_arguments_head"])

    def test_master_key_is_no_user(self):
        s = slp()
        s["metadata"]["user_api_key_user_id"] = "default_user_id"
        s["metadata"]["user_api_key_user_email"] = None
        self.assertIsNone(al.build_request_record(KW, s, "p")["user_id"])

    def test_none_fields_from_litellm(self):
        # LiteLLM は値の無い項目を None で渡すことがある（requester_metadata、usage_object など）
        s = slp(hidden_params=None, error_information=None, response=None)
        s["metadata"].update({"requester_metadata": None, "usage_object": None, "mcp_tool_call_metadata": None})
        kw = {"litellm_call_id": "c", "litellm_params": {"metadata": None, "proxy_server_request": None}}
        self.assertFalse(al.is_internal(kw, s))
        r = al.build_request_record(kw, s, "p")
        self.assertEqual((r["call_id"], r["tool_calls"], r["client"]), ("c", [], "unknown"))

    def test_internal_calls_are_not_recorded(self):
        kw = {"litellm_params": {"metadata": {al.INTERNAL_MARK: True}}}
        self.assertTrue(al.is_internal(kw, slp()))
        self.assertTrue(al.is_internal({}, slp(call_type="adecisions")))
        self.assertFalse(al.is_internal(KW, slp()))

    def test_partition_is_jst(self):
        # UTC 2025-10-11 14:59:59 は日本時間 23 時、15:00:00 は翌日の 0 時
        self.assertEqual(al.partition(1760194799.0), ("2025-10-11", "23"))
        self.assertEqual(al.partition(1760194800.0), ("2025-10-12", "00"))


class SegmentTest(unittest.TestCase):
    def test_range_read_of_one_member_and_whole_file(self):
        buf = al.Buffer("pod-a")
        bodies = [b'{"n": 1}', b'{"n": 2, "x": "' + b"y" * 1000 + b'"}', b'{"n": 3}']
        records = []
        for i, b in enumerate(bodies):
            r = {"call_id": f"c{i}"}
            buf.add(1760194800.0 + i, r, b)
            records.append(r)
        buf.add(1760194800.5, {"type": "review"}, None)
        segs, n = buf.take()
        self.assertEqual((len(segs), n), (1, 4))
        files = dict(segs[0].files())
        body = files[segs[0].body_key]
        for r, b in zip(records, bodies):
            loc = r["body"]
            part = body[loc["offset"]:loc["offset"] + loc["length"]]
            self.assertEqual(gzip.decompress(part), b)
        # 全体も 1 つの gzip として展開できる
        self.assertEqual(gzip.decompress(body), b"".join(bodies))
        lines = gzip.decompress(files[segs[0].meta_key]).decode().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertNotIn("body", json.loads(lines[3]))

    def test_body_written_before_meta_and_names_unique(self):
        buf = al.Buffer("pod-a")
        buf.add(1760194800.0, {"call_id": "a"}, b"{}")
        buf.add(1760194800.0 + 3600, {"call_id": "b"}, b"{}")   # 別の時間は別のファイル
        segs, _ = buf.take()
        self.assertEqual(len(segs), 2)
        self.assertNotEqual(segs[0].base, segs[1].base)
        keys = [k for k, _ in segs[0].files()]
        self.assertTrue(keys[0].startswith("raw/body/") and keys[1].startswith("raw/meta/"))
        self.assertIn("/dt=2025-10-12/hour=00/", keys[0])


class FakeLogger(al.AuditLogger):
    """S3 の代わりに、辞書に書く（fail が真なら失敗する）。"""

    def __init__(self, spool: Path, limit: int = 10 ** 9) -> None:
        super().__init__()   # 環境変数が無いので enabled は偽（スレッドも atexit も始めない）
        self.enabled = True
        self.spool, self.spool_limit = spool, limit
        self.store: dict[str, bytes] = {}
        self.fail = False

    def _put(self, key: str, data: bytes) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = data


class SpoolTest(unittest.TestCase):
    def test_spool_and_resend(self):
        with tempfile.TemporaryDirectory() as d:
            lg = FakeLogger(Path(d))
            lg._record(dict(KW, standard_logging_object=slp()))
            lg.fail = True
            lg.flush()
            self.assertEqual(lg.store, {})
            self.assertTrue(list(Path(d).glob("*.json")))
            lg.fail = False
            lg._resend_spool()
            self.assertEqual(len(lg.store), 2)
            self.assertEqual(lg.stats["written"], 1)
            self.assertFalse(list(Path(d).glob("*")))

    def test_drop_over_limit_and_lost_on_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            lg = FakeLogger(Path(d), limit=10)
            lg._record(dict(KW, standard_logging_object=slp()))
            lg.fail = True
            lg.flush()
            self.assertEqual(lg.stats["dropped"], 1)
            lg.spool_limit = 10 ** 9
            lg._record(dict(KW, standard_logging_object=slp()))
            lg.flush()
            lg.close()   # 書けないまま止まる。件数を警告に出す（例外にしない）
            self.assertEqual(len(list(Path(d).glob("*.json"))), 1)

    def test_internal_and_review(self):
        with tempfile.TemporaryDirectory() as d:
            lg = FakeLogger(Path(d))
            lg._record({"litellm_params": {"metadata": {al.INTERNAL_MARK: True}},
                        "standard_logging_object": slp()})
            lg.record_review({"request_call_id": "call-1", "allowed": False})
            lg.flush()
            meta_key = next(k for k in lg.store if k.startswith("raw/meta/"))
            lines = [json.loads(x) for x in gzip.decompress(lg.store[meta_key]).decode().splitlines()]
            self.assertEqual([x["type"] for x in lines], ["review"])
            self.assertEqual(lines[0]["request_call_id"], "call-1")


class HookTest(unittest.TestCase):
    """成功時の処理の冒頭（async_logging_hook）で記録し、後ろの処理が取り消されても欠けない。"""

    def logger(self, d: str) -> FakeLogger:
        return FakeLogger(Path(d))

    def test_recorded_even_if_later_callbacks_are_cancelled(self):
        import asyncio

        async def success_handler(lg, kwargs):
            # LiteLLM の成功時の処理の形: 全コールバックの async_logging_hook → 遅いコールバック → async_log_success_event
            kwargs, _ = await lg.async_logging_hook(kwargs, None, "acompletion")
            await asyncio.sleep(10)   # Redis を待って止まった前のコールバック
            await lg.async_log_success_event(kwargs, None, None, None)

        async def worker(lg, kwargs):
            # LoggingWorker は 1 つのまとまりを時間で打ち切る（既定 20 秒。ここでは 0.05 秒）
            try:
                await asyncio.wait_for(success_handler(lg, kwargs), timeout=0.05)
            except asyncio.TimeoutError:
                pass

        with tempfile.TemporaryDirectory() as d:
            lg = self.logger(d)
            asyncio.run(worker(lg, dict(KW, standard_logging_object=slp())))
            self.assertEqual(lg.buffer.records, 1)

    def test_hook_and_success_event_record_once(self):
        import asyncio

        async def both(lg, kwargs):
            kwargs, _ = await lg.async_logging_hook(kwargs, None, "acompletion")
            await lg.async_log_success_event(dict(kwargs), None, None, None)   # LiteLLM は写しを渡すことがある

        with tempfile.TemporaryDirectory() as d:
            lg = self.logger(d)
            asyncio.run(both(lg, dict(KW, standard_logging_object=slp())))
            self.assertEqual(lg.buffer.records, 1)

    def test_success_event_records_when_hook_had_no_payload(self):
        import asyncio

        async def run(lg):
            kwargs = dict(KW)   # 冒頭ではまだ本文が無い
            kwargs, _ = await lg.async_logging_hook(kwargs, None, "acompletion")
            kwargs["standard_logging_object"] = slp()
            await lg.async_log_success_event(kwargs, None, None, None)

        with tempfile.TemporaryDirectory() as d:
            lg = self.logger(d)
            asyncio.run(run(lg))
            self.assertEqual(lg.buffer.records, 1)

    def test_no_log_request_is_not_recorded_by_hook(self):
        import asyncio

        with tempfile.TemporaryDirectory() as d:
            lg = self.logger(d)
            kwargs = {"litellm_call_id": "c", "litellm_params": {"no-log": True}, "standard_logging_object": slp()}
            asyncio.run(lg.async_logging_hook(kwargs, None, "acompletion"))
            self.assertEqual(lg.buffer.records, 0)

    def test_stream_recorded_only_when_assembled(self):
        import asyncio

        async def run(lg):
            chunk = dict(KW, stream=True, standard_logging_object=slp())
            await lg.async_logging_hook(chunk, None, "acompletion")   # 途中の断片では記録しない
            self.assertEqual(lg.buffer.records, 0)
            done = dict(chunk, async_complete_streaming_response={"choices": []})
            await lg.async_logging_hook(done, None, "acompletion")
            await lg.async_log_success_event(done, None, None, None)

        with tempfile.TemporaryDirectory() as d:
            lg = self.logger(d)
            asyncio.run(run(lg))
            self.assertEqual(lg.buffer.records, 1)


if __name__ == "__main__":
    unittest.main()
