"""送信するファイルの中身の審査で使う、読み取りの指定・照合・解析（モデルを呼ばない純粋な関数だけを置く）。

機密データチェックの審査官が中身を求めたら、ゲートウェイは送信を止め、エージェントに read_command のコマンドを
そのまま実行させる。次の要求で、会話の最後のツールコールがその読み取りであることを確かめ（verified_reads）、
出力からファイルの中身を切り出して審査官に渡す。

- 照合はツールの名前を見ない。引数の値がコマンドと完全に一致するか（シェルのツール）、
  シェルを 1 回呼んで出力をそのまま返すだけのプログラムか（Codex の exec）で確かめる
- 中身の切り出しは、区切りに書かれたバイト数で行う（中身に区切りの文字列があっても取り違えない）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import transcript as tr

MARK = ": lab-review-read;"
_HEAD = b"\n<<<lab-review-read "
_END = b"\n<<<lab-review-read-end>>>"
_CODEX_PREFIX = "const r = await tools.exec_command("
_CODEX_KEYS = {"cmd", "workdir", "yield_time_ms", "max_output_tokens"}
_CODEX_SUFFIX = re.compile(r"\);\s*text\(r\.output\);\s*")
# 文字として読めないとみなす文字（改行・復帰・タブ以外の制御文字、DEL、置換文字）
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f�]")
# 作業フォルダを変える指定（相対パスのファイルを読ませる前に確かめる。変更仕様書 7.2 節の 5）
_CHDIR = re.compile(r"(?:^|[\s;&|(`{])(?:cd|pushd|popd)(?=$|[\s;&|)`}])|(?:^|\s)(?:-C|--directory|--chdir)(?=$|[\s=])")


@dataclass
class ContentRead:
    """確かめた読み取りの、ファイル 1 つ分の結果。status は ok / too_large / unreadable / missing。"""
    path: str
    status: str
    bytes: int | None = None
    text: str = ""

    def summary(self) -> dict:
        return {"path": self.path, "bytes": self.bytes, "status": self.status}


# ---- 読み取りの指定 ----

def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def quote_path(path: str) -> str:
    """ファイルの指定をシェルの語にする。先頭の ~/ だけはホームフォルダとして扱う。"""
    if "\n" in path or "\r" in path:
        raise ValueError("ファイルの指定に改行が含まれます")
    if path.startswith("~/"):
        return '"$HOME"/' + _sq(path[2:])
    return _sq(path)


def read_command(files: list[str], max_bytes: int) -> str:
    """読み取りのコマンド（変更設計書 4.1 節）。ファイルごとに区切りとバイト数を出し、上限より 1 バイト多く中身を出す。"""
    items = " ".join(quote_path(f) for f in files)
    return (f"{MARK} for p in {items}; do "
            f"""printf '\\n<<<lab-review-read %s>>>\\n' "$(wc -c < "$p" 2>/dev/null | tr -d ' ')"; """
            f'head -c {max_bytes + 1} -- "$p" 2>/dev/null; done; '
            "printf '\\n<<<lab-review-read-end>>>\\n'")


def _unquote_items(s: str) -> list[str] | None:
    """read_command が作ったファイルの並び（'…' か "$HOME"/'…' を空白で区切ったもの）を読み戻す。"""
    out: list[str] = []
    i = 0
    while i < len(s):
        home = s.startswith('"$HOME"/', i)
        if home:
            i += len('"$HOME"/')
        if i >= len(s) or s[i] != "'":
            return None
        i += 1
        buf = []
        while True:
            j = s.find("'", i)
            if j < 0:
                return None
            buf.append(s[i:j])
            if s.startswith("'\\''", j):
                buf.append("'")
                i = j + 4
                continue
            i = j + 1
            break
        out.append(("~/" if home else "") + "".join(buf))
        if i < len(s):
            if s[i] != " ":
                return None
            i += 1
    return out or None


def files_of_command(cmd: str, max_bytes: int) -> list[str] | None:
    """cmd が read_command(…, max_bytes) と完全に一致するなら、そのファイルの並びを返す。"""
    if not isinstance(cmd, str) or not cmd.startswith(MARK + " for p in "):
        return None
    rest = cmd[len(MARK + " for p in "):]
    k = rest.find("; do ")
    if k < 0:
        return None
    files = _unquote_items(rest[:k])
    if not files:
        return None
    try:
        return files if read_command(files, max_bytes) == cmd else None
    except ValueError:
        return None


def codex_program(cmd: str, workdir: str | None, max_output_tokens: int) -> str:
    """Codex の exec で実行させるプログラム（変更設計書 3.6 節）。"""
    obj: dict[str, Any] = {"cmd": cmd}
    if workdir:
        obj["workdir"] = workdir
    obj["max_output_tokens"] = max_output_tokens
    return f"{_CODEX_PREFIX}{json.dumps(obj, ensure_ascii=False)});\ntext(r.output);"


def parse_codex_program(src: Any) -> dict | None:
    """src が「exec_command を 1 回呼んで、その出力をそのまま返すだけ」のプログラムなら、渡しているオブジェクトを返す。"""
    if not isinstance(src, str):
        return None
    s = src.strip()
    if not s.startswith(_CODEX_PREFIX):
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(s, len(_CODEX_PREFIX))
    except ValueError:
        return None
    if not isinstance(obj, dict) or not set(obj) <= _CODEX_KEYS or not isinstance(obj.get("cmd"), str):
        return None
    if not _CODEX_SUFFIX.fullmatch(s, end):
        return None
    return obj


def _codex_objects(src: str) -> list[dict]:
    """プログラムの中の tools.exec_command({...}) のオブジェクトをすべて読む。"""
    out = []
    i = 0
    while True:
        i = src.find("tools.exec_command(", i)
        if i < 0:
            return out
        i += len("tools.exec_command(")
        try:
            obj, _ = json.JSONDecoder().raw_decode(src, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)


def codex_workdirs(pending: list[dict]) -> set[str]:
    """送信が Codex のプログラムなら、その workdir の集合（変更設計書 4.5 節）。"""
    wds: set[str] = set()
    for e in pending:
        for obj in _codex_objects(e["tool_call"]["arguments"]):
            if isinstance(obj.get("workdir"), str):
                wds.add(obj["workdir"])
    return wds


# ---- 読み取りに使うツールの選択（変更仕様書 6.4 節） ----

_SHELL_WORDS = re.compile(r"shell|bash|command|terminal|シェル|コマンド", re.I)
_COMMAND_FIELDS = ("command", "cmd")


@dataclass
class Reader:
    """読み取りのツールコールに使うツール。kind は program（Codex の exec）か command（シェルのツール）。"""
    kind: str
    name: str
    field: str = ""


def _tool_parts(call_type: str, t: dict) -> tuple[str, str, str, Any]:
    """(種類, 名前, 説明, 引数の型)。種類は custom / function / other。"""
    if call_type in tr.RESPONSES_CALL_TYPES:
        kind = t.get("type") if t.get("type") in ("custom", "function") else "other"
        return kind, t.get("name") or "", t.get("description") or "", t.get("parameters")
    if call_type in tr.ANTHROPIC_CALL_TYPES:
        # type を持つのはサーバー側の組み込みのツール（web_search など）で、対象外
        kind = "other" if t.get("type") not in (None, "custom") else "function"
        return kind, t.get("name") or "", t.get("description") or "", t.get("input_schema")
    fn = t.get("function") or {}
    return ("function" if fn else "other"), fn.get("name") or "", fn.get("description") or "", fn.get("parameters")


def _command_field(params: Any) -> str | None:
    """引数の型に command / cmd の文字列の欄があり、必須がその欄だけなら、その欄の名前。"""
    params = tr.to_dict(params)
    if not isinstance(params, dict):
        return None
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    for f in _COMMAND_FIELDS:
        prop = props.get(f)
        if isinstance(prop, dict) and prop.get("type") == "string" and required <= {f}:
            return f
    return None


def select_reader(call_type: str, data: dict) -> Reader | None:
    """リクエストのツールの一覧から、読み取りに使うツールを決まった規則で選ぶ。名前は見ない。"""
    tools = [t for t in tr.request_tools(call_type, data) if isinstance(t, dict)]
    shells: list[Reader] = []
    for t in tools:
        kind, name, desc, params = _tool_parts(call_type, t)
        if not name:
            continue
        if kind == "custom" and "tools.exec_command" in desc:
            return Reader("program", name)
        if kind == "function":
            field = _command_field(params)
            if field and (_SHELL_WORDS.search(name) or _SHELL_WORDS.search(desc)):
                shells.append(Reader("command", name, field))
    return shells[0] if len(shells) == 1 else None


def read_call_input(reader: Reader, files: list[str], max_bytes: int, pending: list[dict]) -> str:
    """読み取りのツールコールの入力（program ならプログラム、command なら引数の JSON）。"""
    cmd = read_command(files, max_bytes)
    if reader.kind == "program":
        wds = codex_workdirs(pending)
        return codex_program(cmd, next(iter(wds)) if len(wds) == 1 else None, (max_bytes + 256) * len(files))
    return json.dumps({reader.field: cmd}, ensure_ascii=False)


# ---- 会話の最後のツールコール（変更仕様書 6.3 節） ----

def _text_of_output(out: Any) -> str:
    out = tr.to_dict(out)
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        return _text_of_output(out.get("content") if "content" in out else out.get("output") or "")
    parts = []
    for p in out or []:
        p = tr.to_dict(p)
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            parts.append(p["text"])
    return "".join(parts)


def _last_responses(inp: Any) -> tuple[Any, str] | None:
    items = [i for i in (tr.to_dict(inp) or []) if isinstance(i, dict)] if not isinstance(inp, str) else []
    idx = max((n for n, i in enumerate(items) if i.get("type") in tr.RESPONSES_TOOL_TYPES), default=-1)
    if idx < 0:
        return None
    call = items[idx]
    args = {"function_call": call.get("arguments"), "custom_tool_call": call.get("input"),
            "local_shell_call": call.get("action")}[call["type"]]
    for i in items[idx + 1:]:
        if i.get("type", "").endswith("_call_output") and i.get("call_id") == call.get("call_id"):
            return args, _text_of_output(i.get("output"))
    return None


def _last_anthropic(messages: Any) -> tuple[Any, str] | None:
    msgs = [m for m in (tr.to_dict(messages) or []) if isinstance(m, dict)]
    last = None
    for n, m in enumerate(msgs):
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    last = (n, b)
    if last is None:
        return None
    n, block = last
    for m in msgs[n + 1:]:
        for b in m.get("content") or [] if isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == block.get("id"):
                return block.get("input"), _text_of_output(b.get("content"))
    return None


def _last_chat(messages: Any) -> tuple[Any, str] | None:
    msgs = [m for m in (tr.to_dict(messages) or []) if isinstance(m, dict)]
    last = None
    for n, m in enumerate(msgs):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                last = (n, tc or {})
    if last is None:
        return None
    n, tc = last
    for m in msgs[n + 1:]:
        if m.get("role") == "tool" and m.get("tool_call_id") == tc.get("id"):
            return (tc.get("function") or {}).get("arguments"), _text_of_output(m.get("content"))
    return None


def last_tool_call(call_type: str, data: dict) -> tuple[Any, str] | None:
    """会話の最後のツールコールの (引数, 出力の文字列)。出力が無ければ None。"""
    if call_type in tr.RESPONSES_CALL_TYPES:
        return _last_responses(data.get("input"))
    if call_type in tr.ANTHROPIC_CALL_TYPES:
        return _last_anthropic(data.get("messages"))
    return _last_chat(data.get("messages"))


def _strings(v: Any) -> list[str]:
    """引数の中の文字列をすべて（JSON の文字列なら読んでから）。"""
    if isinstance(v, str):
        try:
            decoded = json.loads(v)
        except ValueError:
            return [v]
        return [v] + (_strings(decoded) if not isinstance(decoded, str) else [decoded])
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


# ---- 照合と解析 ----

def parse_output(text: str, files: list[str], max_bytes: int) -> list[ContentRead]:
    """読み取りの出力から、ファイルごとの中身を切り出す（変更設計書 4.3 節）。"""
    b = text.encode("utf-8", "surrogatepass")
    pos = b.find(_HEAD)
    if pos < 0 and b.startswith(_HEAD[1:]):  # 先頭の改行を落とすエージェントへの備え
        b, pos = b"\n" + b, 0
    results: list[ContentRead] = []
    broken = pos < 0
    for f in files:
        if broken or not b.startswith(_HEAD, pos):
            broken = True
            results.append(ContentRead(f, "unreadable"))
            continue
        pos += len(_HEAD)
        end = b.find(b">>>\n", pos)
        num = b[pos:end] if end >= 0 else b""
        if end < 0 or not (num == b"" or num.isdigit()):
            broken = True
            results.append(ContentRead(f, "unreadable"))
            continue
        pos = end + 4
        if num == b"":
            results.append(ContentRead(f, "missing"))
            continue
        size = int(num)
        n = min(size, max_bytes + 1)
        chunk = b[pos:pos + n]
        if len(chunk) < n:   # 途中で切れた
            broken = True
            results.append(ContentRead(f, "unreadable", size))
            continue
        pos += n
        if size > max_bytes:
            results.append(ContentRead(f, "too_large", size))
            continue
        try:
            body = chunk.decode("utf-8")
        except UnicodeDecodeError:
            results.append(ContentRead(f, "unreadable", size))
            continue
        results.append(ContentRead(f, "unreadable" if _BAD_CHARS.search(body) else "ok", size,
                                   "" if _BAD_CHARS.search(body) else body))
    if not broken and not b.startswith(_END, pos):
        broken = True
    if broken:  # 終わりの区切りまで読めなければ、どの中身も使わない
        results = [r if r.status in ("missing", "too_large", "unreadable") else ContentRead(r.path, "unreadable", r.bytes)
                   for r in results]
    return results


def verified_reads(call_type: str, data: dict, pending: list[dict], max_bytes: int) -> list[ContentRead]:
    """会話の最後のツールコールが、指定どおりの読み取りなら、その結果を返す。そうでなければ空（変更仕様書 6.2 節）。"""
    last = last_tool_call(call_type, data)
    if last is None:
        return []
    args, output = last
    wds = codex_workdirs(pending)
    files = None
    prog = parse_codex_program(args)
    if prog is not None:
        if len(wds) == 1 and prog.get("workdir") != next(iter(wds)):
            return []
        files = files_of_command(prog["cmd"], max_bytes)
    else:
        for s in _strings(args):
            files = files_of_command(s, max_bytes)
            if files:
                break
    if not files:
        return []
    return parse_output(output, files, max_bytes)


def _arg_strings(pending: list[dict]) -> list[str]:
    out: list[str] = []
    for e in pending:
        out += _strings(e["tool_call"]["arguments"])
        for obj in _codex_objects(e["tool_call"]["arguments"]):
            if isinstance(obj.get("cmd"), str):
                out.append(obj["cmd"])
    return out


def changes_directory(pending: list[dict]) -> bool:
    """送信の引数に、作業フォルダを変える指定があるか（変更設計書 4.6 節）。"""
    strings = []
    for e in pending:
        objs = _codex_objects(e["tool_call"]["arguments"])
        if objs:   # Codex のプログラムは cmd だけを見る（workdir は照合で一致を求める）
            strings += [o["cmd"] for o in objs if isinstance(o.get("cmd"), str)]
        else:
            strings += _strings(e["tool_call"]["arguments"])
    return any(_CHDIR.search(s) for s in strings)


def is_relative(path: str) -> bool:
    return not (path.startswith("/") or path.startswith("~/"))


def request_problem(files: Any, pending: list[dict], reads: list[ContentRead], max_files: int) -> str | None:
    """審査官の need_content を、読ませずに止める理由（変更仕様書 7.2 節）。問題が無ければ None。"""
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        return "審査官が中身を求めたが、読むファイルを示さなかったため"
    if len(files) > max_files:
        return f"送信するファイルが多すぎて（{len(files)} 個。上限 {max_files} 個）、中身を確かめられないため"
    args = _arg_strings(pending)
    already = {r.path for r in reads}
    for f in files:
        if "\n" in f or "\r" in f:
            return f"ファイルの指定 {f!r} に改行が含まれ、読み取りを指定できないため"
        if not any(f in s for s in args):
            return f"審査官が求めたファイル {f} が、送信の引数に見当たらないため"
        if f in already:
            return f"ファイル {f} の中身を確かめても判定できなかったため"
        if is_relative(f) and changes_directory(pending):
            return f"送信の中で作業フォルダを変えているため、相対パスのファイル {f} の中身を確かめられないため"
    return None


def status_reason(r: ContentRead, max_bytes: int) -> str:
    """確かめた読み取りが ok でないときの、止める理由（変更設計書 4.4 節）。"""
    if r.status == "too_large":
        return f"送信するファイル {r.path} が大きすぎて（{r.bytes} バイト。上限 {max_bytes} バイト）、中身を確かめられないため"
    if r.status == "missing":
        return f"送信するファイル {r.path} を読めず、中身を確かめられないため"
    return f"送信するファイル {r.path} を文字として読めず、中身を確かめられないため"
