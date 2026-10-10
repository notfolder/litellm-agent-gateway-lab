"""判定入力の抽出（モデルを呼ばない純粋な関数だけを置く）。

auto mode の「推論ブラインド」にならい、会話からユーザーの発言とツールコール本体だけを抜き出す。
アシスタントの文章、推論、ツールの結果は捨てる。

対応する形式（LiteLLM の call_type）:
- aresponses / responses: OpenAI Responses API（Codex）
- acompletion / completion: Chat Completions
- anthropic_messages: Anthropic Messages
"""

from __future__ import annotations

import json
from typing import Any

MAX_TEXT = 2000          # 1 件あたりの文字数の上限
MAX_ENTRIES = 40         # 判定入力に含める件数の上限（新しいものから）

RESPONSES_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
RESPONSES_CALL_TYPES = ("aresponses", "responses")
CHAT_CALL_TYPES = ("acompletion", "completion")
ANTHROPIC_CALL_TYPES = ("anthropic_messages",)


def to_dict(obj: Any) -> Any:
    """pydantic のオブジェクトなどを dict に揃える。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json", exclude_none=True)
        except TypeError:
            return dump()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"…（{len(text) - MAX_TEXT} 文字省略）"


def _text_of_content(content: Any) -> str:
    """content（文字列、またはパーツの配列）から文章だけを取り出す。"""
    content = to_dict(content)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") in ("input_text", "text", "output_text") and isinstance(p.get("text"), str):
            parts.append(p["text"])
    return "\n".join(parts)


def _args_str(args: Any) -> str:
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def tool_call_entry(name: str, arguments: Any) -> dict:
    return {"tool_call": {"name": name or "(不明)", "arguments": _clip(_args_str(arguments))}}


def responses_item_to_call(item: dict) -> dict | None:
    t = item.get("type")
    if t == "function_call":
        return tool_call_entry(item.get("name", ""), item.get("arguments", ""))
    if t == "custom_tool_call":
        return tool_call_entry(item.get("name", ""), item.get("input", ""))
    if t == "local_shell_call":
        return tool_call_entry("local_shell", item.get("action", {}))
    return None


# ---- 会話（リクエスト）からの抽出 ----

def transcript_from_request(call_type: str, data: dict) -> list[dict]:
    if call_type in RESPONSES_CALL_TYPES:
        entries = _from_responses_input(data.get("input"))
    elif call_type in ANTHROPIC_CALL_TYPES:
        entries = _from_anthropic_messages(data.get("messages"))
    else:
        entries = _from_chat_messages(data.get("messages"))
    return entries[-MAX_ENTRIES:]


def _from_responses_input(inp: Any) -> list[dict]:
    inp = to_dict(inp)
    if isinstance(inp, str):
        return [{"user": _clip(inp)}]
    out: list[dict] = []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user" and item.get("type", "message") == "message":
            text = _text_of_content(item.get("content"))
            if text:
                out.append({"user": _clip(text)})
            continue
        call = responses_item_to_call(item)
        if call:
            out.append(call)
    return out


def _from_chat_messages(messages: Any) -> list[dict]:
    out: list[dict] = []
    for m in to_dict(messages) or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            text = _text_of_content(m.get("content"))
            if text:
                out.append({"user": _clip(text)})
        elif m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                out.append(tool_call_entry(fn.get("name", ""), fn.get("arguments", "")))
    return out


def _from_anthropic_messages(messages: Any) -> list[dict]:
    out: list[dict] = []
    for m in to_dict(messages) or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if m.get("role") == "user":
            # tool_result は「ツールの結果」なので捨て、文章だけを残す
            text = _text_of_content(content)
            if text:
                out.append({"user": _clip(text)})
        elif m.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    out.append(tool_call_entry(block.get("name", ""), block.get("input", {})))
    return out


# ---- 応答（非ストリーミング）からのツールコール抽出 ----

def tool_calls_from_response(call_type: str, response: Any) -> list[dict]:
    r = to_dict(response)
    if not isinstance(r, dict):
        return []
    if call_type in RESPONSES_CALL_TYPES:
        return [c for c in (responses_item_to_call(to_dict(i)) for i in r.get("output") or []) if c]
    if call_type in ANTHROPIC_CALL_TYPES:
        return [tool_call_entry(b.get("name", ""), b.get("input", {}))
                for b in r.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
    calls: list[dict] = []
    for ch in r.get("choices") or []:
        msg = (ch or {}).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            calls.append(tool_call_entry(fn.get("name", ""), fn.get("arguments", "")))
    return calls


def render_for_judge(transcript: list[dict], pending: list[dict]) -> str:
    """二次判定（LLM）に渡す文章。"""
    lines = ["# これまでの会話（ユーザーの発言と、実行済みのツールコールのみ）"]
    for e in transcript:
        if "user" in e:
            lines.append(f"[ユーザー] {e['user']}")
        else:
            tc = e["tool_call"]
            lines.append(f"[ツール実行済み] {tc['name']} {tc['arguments']}")
    lines.append("")
    lines.append("# 審査対象（これから実行しようとしているツールコール）")
    for e in pending:
        tc = e["tool_call"]
        lines.append(f"- {tc['name']} {tc['arguments']}")
    return "\n".join(lines)


def summarize_calls(pending: list[dict]) -> str:
    return ", ".join(f"{e['tool_call']['name']} {e['tool_call']['arguments'][:120]}" for e in pending)
