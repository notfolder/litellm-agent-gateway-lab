"""中身の審査で使う、読み取りの指定・照合・解析（モデルを呼ばない純粋な関数だけを置く）。

ゲートウェイはモデルへのリクエストに方針を足し、ファイルを外へ送る前・名指しのプログラムを動かす（作る・置く）前に、
read_command のコマンドをモデル自身に実行させる。送信や実行の要求が来たら、会話の最後のツールコールがその読み取りで
あることを確かめ（verified_reads）、出力からファイルの中身と環境変数の値を切り出して審査官に渡す。
読まずに送った・動かしたときは、審査官の need_content を受けて拒否の文で読み取りを案内する（request_problem）。

- 照合はツールの名前を見ない。引数の値がコマンドと完全に一致するか（シェルのツール）、
  シェルを 1 回呼んで出力をそのまま返すだけのプログラムか（Codex の exec）で確かめる
- 中身の切り出しは、区切りに書かれたバイト数で行う（中身に区切りの文字列があっても取り違えない）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import transcript as tr

MARK = ": lab-review-read;"
_HEAD = b"\n<<<lab-review-read "
_ENV_HEAD = b"\n<<<lab-review-env "
_END = b"\n<<<lab-review-read-end>>>"
_CODEX_PREFIX = "const r = await tools.exec_command("
_CODEX_KEYS = {"cmd", "workdir", "yield_time_ms", "max_output_tokens"}
_CODEX_SUFFIX = re.compile(r"\);\s*text\(r\.output\);\s*")
# 文字として読めないとみなす文字（改行・復帰・タブ以外の制御文字、DEL、置換文字）
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f�]")
# 作業フォルダを変える指定（相対パスのファイルを読ませる前に確かめる。変更仕様書 7.2 節の 5）
_CHDIR = re.compile(r"(?:^|[\s;&|(`{])(?:cd|pushd|popd)(?=$|[\s;&|)`}])|(?:^|\s)(?:-C|--directory|--chdir)(?=$|[\s=])")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 止めたときに添える直し方の案内（審査官が advice の種類を返し、ゲートウェイが文を持つ。変更設計書 4.4 節）
ADVICE = {
    "name": "動かすプログラムや作る元のファイルを名指しで書き直してください",
    "tree": "作業中の木の中で行ってください",
    "script": "一時的な処理は作業中の木の中にスクリプト（文字のファイル）として書いてください",
}
BUILD_ADVICE = "ビルドスクリプトか Makefile を作業中の木に置いて、それを使ってください"


@dataclass
class ReadSpec:
    """読み取りの指定: ファイルの並びと、環境変数の名前の並び。"""
    files: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.files or self.env)

    def count(self) -> int:
        return len(self.files) + len(self.env)


@dataclass
class ContentRead:
    """確かめた読み取りの、項目 1 つ分の結果。
    status は ok / too_large / missing / unreadable（出力が壊れている）/ binary（文字として読めない）/
    binary_artifact（審査官が生成物と分類した binary）。kind は file か env（path は env:名前）。"""
    path: str
    status: str
    bytes: int | None = None
    text: str = ""
    resolved: str = ""
    in_args: bool = False
    kind: str = "file"

    def summary(self) -> dict:
        return {"path": self.path, "bytes": self.bytes, "status": self.status, "in_args": self.in_args}


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


def read_command(files: list[str], max_bytes: int, env: list[str] | tuple[str, ...] = ()) -> str:
    """読み取りのコマンド（変更設計書 4.1 節）。ファイルごとに区切り（バイト数と解決済みの絶対パス）を出してから
    上限より 1 バイト多く中身を出し、環境変数ごとに区切り（名前とバイト数）と値を出す。"""
    if not files and not env:
        raise ValueError("読み取りの指定が空です")
    parts = [MARK]
    if files:
        items = " ".join(quote_path(f) for f in files)
        parts.append(
            f"for p in {items}; do "
            """printf '\\n<<<lab-review-read %s %s>>>\\n' "$(wc -c < "$p" 2>/dev/null | tr -d ' ')" """
            """"$(cd "$(dirname -- "$p")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename -- "$p")")"; """
            f'head -c {max_bytes + 1} -- "$p" 2>/dev/null; done;')
    if env:
        for n in env:
            if not ENV_NAME.match(n):
                raise ValueError(f"環境変数の名前が不正です: {n!r}")
        parts.append(
            f"for v in {' '.join(env)}; do "
            """printf '\\n<<<lab-review-env %s %s>>>\\n' "$v" "$(printenv "$v" | wc -c | tr -d ' ')"; """
            'printenv "$v"; done;')
    parts.append("printf '\\n<<<lab-review-read-end>>>\\n'")
    return " ".join(parts)


def _parse_items(s: str, i: int) -> tuple[list[str], int]:
    """read_command が作ったファイルの並び（'…' か "$HOME"/'…' を空白で区切ったもの）を位置 i から読み戻す。
    並びの終わり（引用符で始まらない所）で止め、(項目, 終わりの位置) を返す。"""
    out: list[str] = []
    while i < len(s):
        home = s.startswith('"$HOME"/', i)
        j = i + len('"$HOME"/') if home else i
        if j >= len(s) or s[j] != "'":
            break
        j += 1
        buf = []
        while True:
            k = s.find("'", j)
            if k < 0:
                return out, i
            buf.append(s[j:k])
            if s.startswith("'\\''", k):
                buf.append("'")
                j = k + 4
                continue
            j = k + 1
            break
        out.append(("~/" if home else "") + "".join(buf))
        i = j
        if s.startswith(" ", i) and (s.startswith(" '", i) or s.startswith(' "$HOME"/', i)):
            i += 1
            continue
        break
    return out, i


_ENV_TAIL = re.compile(r" for v in ([A-Za-z_][A-Za-z0-9_]*(?: [A-Za-z_][A-Za-z0-9_]*)*); do printf "
                       r"'\\n<<<lab-review-env %s %s>>>\\n' \"\$v\" \"\$\(printenv \"\$v\" \| wc -c \| tr -d ' '\)\"; "
                       r"printenv \"\$v\"; done; printf '\\n<<<lab-review-read-end>>>\\n'$")


def spec_of_command(cmd: Any, max_bytes: int) -> ReadSpec | None:
    """cmd が read_command(…, max_bytes, …) と完全に一致するなら、その指定を返す。"""
    if not isinstance(cmd, str) or not cmd.startswith(MARK + " "):
        return None
    rest = cmd[len(MARK) + 1:]
    files: list[str] = []
    if rest.startswith("for p in "):
        files, end = _parse_items(rest, len("for p in "))
        if not files or not rest.startswith("; do ", end):
            return None
    m = _ENV_TAIL.search(cmd)
    env = m.group(1).split(" ") if m else []
    try:
        return ReadSpec(files, env) if read_command(files, max_bytes, env) == cmd else None
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
    """審査対象が Codex のプログラムなら、その workdir の集合（変更設計書 4.5 節）。"""
    wds: set[str] = set()
    for e in pending:
        for obj in _codex_objects(e["tool_call"]["arguments"]):
            if isinstance(obj.get("workdir"), str):
                wds.add(obj["workdir"])
    return wds


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

def _text_status(chunk: bytes) -> tuple[str, str]:
    """中身のバイト列が文字として読めれば ("ok", 文字列)、読めなければ ("binary", "")。"""
    try:
        body = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", ""
    return ("binary", "") if _BAD_CHARS.search(body) else ("ok", body)


def parse_output(text: str, spec: ReadSpec, max_bytes: int) -> list[ContentRead]:
    """読み取りの出力から、項目ごとの中身を切り出す（変更設計書 4.3 節）。"""
    b = text.encode("utf-8", "surrogatepass")
    first = _HEAD if spec.files else _ENV_HEAD
    pos = b.find(first)
    if pos < 0 and b.startswith(first[1:]):  # 先頭の改行を落とすエージェントへの備え
        b, pos = b"\n" + b, 0
    results: list[ContentRead] = []
    broken = pos < 0
    for f in spec.files:
        if broken or not b.startswith(_HEAD, pos):
            broken = True
            results.append(ContentRead(f, "unreadable"))
            continue
        pos += len(_HEAD)
        end = b.find(b">>>\n", pos)
        header = b[pos:end] if end >= 0 else b""
        num, _, resolved_b = header.partition(b" ")
        if end < 0 or not (num == b"" or num.isdigit()):
            broken = True
            results.append(ContentRead(f, "unreadable"))
            continue
        resolved = resolved_b.decode("utf-8", "replace")
        pos = end + 4
        if num == b"":
            results.append(ContentRead(f, "missing", resolved=resolved))
            continue
        size = int(num)
        n = min(size, max_bytes + 1)
        chunk = b[pos:pos + n]
        if len(chunk) < n:   # 途中で切れた
            broken = True
            results.append(ContentRead(f, "unreadable", size, resolved=resolved))
            continue
        pos += n
        if size > max_bytes:
            results.append(ContentRead(f, "too_large", size, resolved=resolved))
            continue
        status, body = _text_status(chunk)
        results.append(ContentRead(f, status, size, body, resolved=resolved))
    for name in spec.env:
        path = f"env:{name}"
        head = _ENV_HEAD + name.encode() + b" "
        if broken or not b.startswith(head, pos):
            broken = True
            results.append(ContentRead(path, "unreadable", kind="env"))
            continue
        pos += len(head)
        end = b.find(b">>>\n", pos)
        num = b[pos:end] if end >= 0 else b""
        if end < 0 or not num.isdigit():
            broken = True
            results.append(ContentRead(path, "unreadable", kind="env"))
            continue
        pos = end + 4
        size = int(num)
        chunk = b[pos:pos + size]
        if len(chunk) < size:
            broken = True
            results.append(ContentRead(path, "unreadable", size, kind="env"))
            continue
        pos += size
        results.append(ContentRead(path, "ok", size, chunk.decode("utf-8", "replace"), kind="env"))
    if not broken and not b.startswith(_END, pos):
        broken = True
    if broken:  # 終わりの区切りまで読めなければ、どの中身も使わない
        results = [r if r.status in ("missing", "too_large", "unreadable")
                   else ContentRead(r.path, "unreadable", r.bytes, resolved=r.resolved, kind=r.kind)
                   for r in results]
    return results


def _arg_strings(pending: list[dict]) -> list[str]:
    out: list[str] = []
    for e in pending:
        out += _strings(e["tool_call"]["arguments"])
        for obj in _codex_objects(e["tool_call"]["arguments"]):
            if isinstance(obj.get("cmd"), str):
                out.append(obj["cmd"])
    return out


def _in_args(item: str, args: list[str]) -> bool:
    return any(item in s for s in args)


def verified_reads(call_type: str, data: dict, pending: list[dict], max_bytes: int) -> list[ContentRead]:
    """会話の最後のツールコールが、指定どおりの読み取りなら、その結果を返す。そうでなければ空（変更仕様書 6.2 節）。
    項目ごとに、審査対象の引数に現れるか（in_args）を付ける。ファイルの項目が 1 つも引数に無い読み取りは使わない。"""
    last = last_tool_call(call_type, data)
    if last is None:
        return []
    args_in, output = last
    wds = codex_workdirs(pending)
    spec = None
    prog = parse_codex_program(args_in)
    if prog is not None:
        spec = spec_of_command(prog["cmd"], max_bytes)
        # 実行する場所（workdir）が審査対象と違う読み取りは、相対パスの項目があるときだけ外す（絶対パスは場所に依らない）
        if spec and len(wds) == 1 and prog.get("workdir") != next(iter(wds)) and any(is_relative(f) for f in spec.files):
            return []
    else:
        for s in _strings(args_in):
            spec = spec_of_command(s, max_bytes)
            if spec:
                break
    if not spec:
        return []
    args = _arg_strings(pending)
    flags = [_in_args(f, args) for f in spec.files]
    if not any(flags):
        return []
    if any(is_relative(f) for f in spec.files) and changes_directory(pending):
        return []
    reads = parse_output(output, spec, max_bytes)
    if any(r.status == "unreadable" for r in reads):
        # 出力が壊れている（途中で切れた、形が違う、エージェントがエラーの文を返した）ときは、読み取りが無かったものとして扱う。
        # 送信や実行なら審査官が中身を求め、読み直しの案内になる。読み取りの後の無関係な操作を止めないため
        return []
    for r in reads:
        if r.kind == "file":
            r.in_args = _in_args(r.path, args)
    return reads


def changes_directory(pending: list[dict]) -> bool:
    """審査対象の引数に、作業フォルダを変える指定があるか（変更設計書 4.6 節）。"""
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


def _str_list(v: Any) -> list[str] | None:
    if v is None:
        return []
    if isinstance(v, list) and all(isinstance(x, str) and x for x in v):
        return list(v)
    return None


def request_problem(files: Any, runs: Any, env: Any, pending: list[dict], reads: list[ContentRead],
                    max_files: int) -> tuple[str | None, ReadSpec | None]:
    """審査官の need_content を確かめる（変更仕様書 7.2 節）。
    止める理由（None なら問題なし）と、拒否の文で示す読み取りの指定（今回の読み取りの項目に新しい項目を足したもの）を返す。"""
    files, runs, env = _str_list(files), _str_list(runs), _str_list(env)
    if files is None or runs is None or env is None:
        return "審査官が中身を求めたが、読む項目の形が不正なため", None
    if not (files or runs or env):
        return "審査官が中身を求めたが、読むファイルを示さなかったため", None
    for f in files + runs:
        if "\n" in f or "\r" in f:
            return f"ファイルの指定 {f!r} に改行が含まれ、読み取りを指定できないため", None
    for n in env:
        if not ENV_NAME.match(n):
            return f"環境変数の名前 {n!r} が不正で、読み取りを指定できないため", None
    args = _arg_strings(pending)
    for f in files:
        if not _in_args(f, args):
            return f"審査官が求めたファイル {f} が、送信の引数に見当たらないため", None
    already = {r.path for r in reads}
    new_files = [f for f in files + runs if f not in already]
    new_env = [n for n in env if f"env:{n}" not in already]
    if not new_files and not new_env:
        first = (files + runs + [f"env:{n}" for n in env])[0]
        return f"ファイル {first} の中身を確かめても判定できなかったため", None
    if runs and not any(_in_args(r, args) for r in runs) and not any(r.kind == "file" and r.in_args for r in reads):
        return "審査官の答えが不正なため（プログラムの中身なしに取り込み先を求めた）", None
    chdir = changes_directory(pending)
    for f in new_files:
        if is_relative(f) and chdir:
            return (f"送信の中で作業フォルダを変えているため、相対パスのファイル {f} の中身を確かめられないため。"
                    f"作業フォルダを移さず、ファイルを絶対パスで指定して送り直してください"), None
    spec = ReadSpec([r.path for r in reads if r.kind == "file"] + new_files,
                    [r.path[4:] for r in reads if r.kind == "env"] + new_env)
    if spec.count() > max_files:
        reason = f"読ませるファイルが多すぎて（{spec.count()} 個。上限 {max_files} 個）、中身を確かめられないため"
        if runs:
            reason += f"。{BUILD_ADVICE}"
        return reason, None
    return None, spec


def status_reason(r: ContentRead, max_bytes: int) -> str:
    """確かめた読み取りが ok でないときの、止める理由（変更設計書 4.4 節）。"""
    if r.status == "too_large":
        return f"ファイル {r.path} が大きすぎて（{r.bytes} バイト。上限 {max_bytes} バイト）、中身を確かめられないため"
    if r.status == "missing":
        return f"ファイル {r.path} を読めず、中身を確かめられないため"
    if r.status == "binary":
        return f"{r.path} を文字として読めず、中身を確かめられないため。{ADVICE['script']}"
    return f"{r.path} の読み取りの出力が壊れていて（途中で切れた、形が違う）、中身を確かめられないため"   # 今は呼ばれない（壊れた読み取りは無かったものとして扱う）
