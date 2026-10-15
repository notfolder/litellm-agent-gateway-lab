"""審査ガードレールの試験用の偽 LLM サーバー（標準ライブラリだけで動く）。

OpenAI 互換の 2 つの API を、ストリーミングあり・なしの両方で返す。
- POST /v1/chat/completions
- POST /v1/responses

最後のユーザー発言に次の形の指示があれば、ツールコールを返す。なければ短い文章を返す。
    TOOL:<ツール名>:<引数の JSON>
例: TOOL:shell:{"command":["git","push","origin","--delete","old"]}
"""

from __future__ import annotations

import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_RE = re.compile(r"TOOL:([A-Za-z0-9_\-]+):(\{.*\})", re.DOTALL)


def _last_user_text_chat(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _last_user_text_responses(inp) -> str:
    if isinstance(inp, str):
        return inp
    for item in reversed(inp or []):
        if item.get("role") == "user" or item.get("type") == "message" and item.get("role") == "user":
            c = item.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


JUDGE_MARKER = "[[tool-call-review]]"
# 審査官の役: 審査対象にこれらの語があれば block を返す（試験用の単純な規則）
BLOCK_WORDS = ("--delete", "rm -rf", "\"rm\",\"-rf\"", "gist", "| bash", "|bash", "--force", "push -f",
               "--no-verify", "--skip", "token", "credentials", ".env")


ISOLATED_MARKER = "[[tool-call-isolated-review]]"
# 文脈独立審査の役: 機密の語と外部への送信の語の両方があれば block を返す（試験用の単純な規則）
SECRET_WORDS = ("sk-", "akia", "begin private key", "confidential")
SEND_WORDS = ("curl", "gist", "https://")


CONTENT_SECTION = "# 送信されるファイルの中身"
NEEDCHECK_RE = re.compile(r"[\w./~-]+\.needcheck")


def _isolated_reply(texts: list[str]) -> str:
    # 送信するファイルの中身の審査の役: .needcheck のファイルを送るなら、中身が無ければ求め、あれば SECRET の有無で決める
    text = texts[-1]
    pending = text.split("# 審査対象", 1)[-1].split("# 審査対象のツールの定義", 1)[0]
    m = NEEDCHECK_RE.search(pending)
    if m:
        if CONTENT_SECTION not in text:
            return json.dumps({"decision": "need_content", "files": [m.group(0)],
                               "reason": "試験用の審査官: 中身を見ないと判定できません"}, ensure_ascii=False)
        if "SECRET" in text.split(CONTENT_SECTION, 1)[1]:
            return json.dumps({"decision": "block", "reason": "試験用の審査官: 中身に SECRET があります"},
                              ensure_ascii=False)
        return json.dumps({"decision": "allow", "reason": "試験用の審査官: 中身に機密なし"}, ensure_ascii=False)
    # 審査対象の部分だけを見る（ツールの定義の説明文の中の語で判定が変わらないように）
    target = texts[-1].split("# 審査対象", 1)[-1].split("# 審査対象のツールの定義", 1)[0].lower()
    secret = next((w for w in SECRET_WORDS if w in target), None)
    send = next((w for w in SEND_WORDS if w in target), None)
    if secret and send:
        return json.dumps({"decision": "block",
                           "reason": f"試験用の審査官: 機密の語「{secret}」を「{send}」で外部に送る操作は止めます"},
                          ensure_ascii=False)
    return json.dumps({"decision": "allow", "reason": "試験用の審査官: 機密の持ち出しなし"}, ensure_ascii=False)


def _judge_reply(messages: list) -> str | None:
    texts = [m.get("content") for m in messages or [] if isinstance(m.get("content"), str)]
    # 文脈独立審査の印を先に調べる（既存の印の判定が先に当たらないように）
    if any(ISOLATED_MARKER in t for t in texts):
        return _isolated_reply(texts)
    if not any(JUDGE_MARKER in t for t in texts):
        return None
    target = texts[-1].split("# 審査対象", 1)[-1].lower()
    for w in BLOCK_WORDS:
        if w.lower() in target:
            return json.dumps({"decision": "block", "reason": f"試験用の審査官: 「{w}」を含む操作は止めます"},
                              ensure_ascii=False)
    return json.dumps({"decision": "allow", "reason": "試験用の審査官: 問題なし"}, ensure_ascii=False)


def _tool_output_text(items: list) -> str | None:
    """最後の項目がツールの結果なら、その中身を返す（エージェントは毎ターン会話全体を送るので、
    これを見ないと同じツールコールを繰り返してしまう）。"""
    last = (items or [None])[-1]
    if not isinstance(last, dict):
        return None
    if last.get("type") == "function_call_output":
        out = last.get("output")
    elif last.get("role") == "tool":
        out = last.get("content")
    else:
        return None
    if not isinstance(out, str):
        out = json.dumps(out, ensure_ascii=False)
    if "<<<lab-review-read" in out:
        # ゲートウェイの指定どおりにファイルを読んだあと。エージェントと同じく、同じ送信をもう一度行う
        return None
    return "tool result: " + out[:2000]


def _tool_names(tools: list) -> list[str]:
    names = []
    for t in tools or []:
        if isinstance(t, dict):
            names.append(t.get("name") or (t.get("function") or {}).get("name") or t.get("type", "?"))
    return names


def _plan(text: str):
    m = TOOL_RE.search(text)
    if m:
        return ("tool", m.group(1), m.group(2))
    return ("text", "fake response", None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802 - 標準の名前
        print("fake-llm:", fmt % args, flush=True)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, data, event: str | None = None) -> None:
        out = ""
        if event:
            out += f"event: {event}\n"
        out += "data: " + (data if isinstance(data, str) else json.dumps(data)) + "\n\n"
        self.wfile.write(out.encode())
        self.wfile.flush()

    def do_GET(self):  # noqa: N802
        if self.path in ("/health", "/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": "fake-tools", "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.split("?")[0]
        if path.endswith("/chat/completions"):
            self._chat(req)
        elif path.endswith("/responses"):
            self._responses(req)
        else:
            self._json(404, {"error": f"unknown path {path}"})

    # ---- Chat Completions ----
    def _chat(self, req) -> None:
        judged = _judge_reply(req.get("messages")) or _tool_output_text(req.get("messages"))
        kind, a, b = ("text", judged, None) if judged else _plan(_last_user_text_chat(req.get("messages")))
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        model = req.get("model", "fake-tools")
        call_id = "call_" + uuid.uuid4().hex[:12]
        if not req.get("stream"):
            if kind == "tool":
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": call_id, "type": "function", "function": {"name": a, "arguments": b}}]}
                finish = "tool_calls"
            else:
                msg = {"role": "assistant", "content": a}
                finish = "stop"
            self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            return
        self._sse_start()
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        self._sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        if kind == "tool":
            self._sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": call_id, "type": "function", "function": {"name": a, "arguments": ""}}]},
                "finish_reason": None}]})
            half = len(b) // 2
            for part in (b[:half], b[half:]):
                self._sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": part}}]}, "finish_reason": None}]})
            finish = "tool_calls"
        else:
            self._sse({**base, "choices": [{"index": 0, "delta": {"content": a}, "finish_reason": None}]})
            finish = "stop"
        self._sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self._sse("[DONE]")

    # ---- Responses API ----
    def _responses(self, req) -> None:
        if req.get("tools"):
            print("fake-llm: tools offered:", ",".join(_tool_names(req["tools"]))[:3000], flush=True)
        inp = req.get("input")
        result = _tool_output_text(inp) if isinstance(inp, list) else None
        kind, a, b = ("text", result, None) if result else _plan(_last_user_text_responses(inp))
        rid = "resp_" + uuid.uuid4().hex[:12]
        created = int(time.time())
        model = req.get("model", "fake-tools")
        if kind == "tool":
            item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:12],
                    "call_id": "call_" + uuid.uuid4().hex[:12], "name": a, "arguments": b, "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:12], "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": a, "annotations": []}]}
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}

        def resp_obj(status, output):
            return {"id": rid, "object": "response", "created_at": created, "status": status, "model": model,
                    "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                    "usage": usage if status == "completed" else None}

        if not req.get("stream"):
            self._json(200, resp_obj("completed", [item]))
            return
        self._sse_start()
        seq = iter(range(1000))
        self._sse({"type": "response.created", "sequence_number": next(seq), "response": resp_obj("in_progress", [])},
                  event="response.created")
        added = dict(item)
        if kind == "tool":
            added = {**item, "arguments": "", "status": "in_progress"}
        else:
            added = {**item, "content": [], "status": "in_progress"}
        self._sse({"type": "response.output_item.added", "sequence_number": next(seq), "output_index": 0,
                   "item": added}, event="response.output_item.added")
        if kind == "tool":
            half = len(b) // 2
            for part in (b[:half], b[half:]):
                self._sse({"type": "response.function_call_arguments.delta", "sequence_number": next(seq),
                           "item_id": item["id"], "output_index": 0, "delta": part},
                          event="response.function_call_arguments.delta")
            self._sse({"type": "response.function_call_arguments.done", "sequence_number": next(seq),
                       "item_id": item["id"], "output_index": 0, "arguments": b},
                      event="response.function_call_arguments.done")
        else:
            self._sse({"type": "response.content_part.added", "sequence_number": next(seq), "item_id": item["id"],
                       "output_index": 0, "content_index": 0,
                       "part": {"type": "output_text", "text": "", "annotations": []}},
                      event="response.content_part.added")
            self._sse({"type": "response.output_text.delta", "sequence_number": next(seq), "item_id": item["id"],
                       "output_index": 0, "content_index": 0, "delta": a},
                      event="response.output_text.delta")
            self._sse({"type": "response.output_text.done", "sequence_number": next(seq), "item_id": item["id"],
                       "output_index": 0, "content_index": 0, "text": a},
                      event="response.output_text.done")
            self._sse({"type": "response.content_part.done", "sequence_number": next(seq), "item_id": item["id"],
                       "output_index": 0, "content_index": 0,
                       "part": {"type": "output_text", "text": a, "annotations": []}},
                      event="response.content_part.done")
        self._sse({"type": "response.output_item.done", "sequence_number": next(seq), "output_index": 0,
                   "item": item}, event="response.output_item.done")
        self._sse({"type": "response.completed", "sequence_number": next(seq),
                   "response": resp_obj("completed", [item])}, event="response.completed")


def main() -> None:
    import os

    port = int(os.environ.get("PORT", "8000"))
    print(f"fake-llm listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
