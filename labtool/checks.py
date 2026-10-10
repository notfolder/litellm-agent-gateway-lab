"""試験: 疎通確認、冪等性、負荷、審査の判定ログの集計。"""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import time
from collections import Counter

from .core import (
    KUBE_CONTEXT, NS, ROOT, Env, LabError, helm, http, http_ok, info, json_output, kn, ok, run, step, warn,
)

BAD = 'TOOL:shell:{"command":["git","push","origin","--delete","old-branch"]}'
GOOD = 'TOOL:shell:{"command":["pytest","-q"]}'
CLAUDE_UA = "claude-cli/2.1.296 (external, cli)"


def smoke(env: Env) -> None:
    """疎通確認。偽 LLM（fake-tools）を使うので、ChatGPT や Claude の利用枠を使わない。
    1. 入口から LiteLLM に届く
    2. パススルーの経路が 403 で閉じている
    3. 審査: 危険なツールコールは「拒否: 理由」に差し替わり、安全なものは通る（3 形式 × ストリーミング有無）
    4. Claude Code の User-Agent の Anthropic 形式は審査されない（素通し）
    5. 検証ユーザーのキーが使える（ユーザーを設定しているとき）
    """
    step("test-smoke 疎通確認")
    base, key = env.base_url, env.get("LITELLM_MASTER_KEY")
    results: list[bool] = []

    def check(desc: str, passed) -> None:
        try:
            good = bool(passed())
        except Exception as e:  # noqa: BLE001 試験の失敗として数える
            good = False
            desc += f"（{e}）"
        (ok if good else warn)(f"{'OK' if good else 'NG'}  {desc}")
        results.append(good)

    def responses(text: str, stream: bool) -> str:
        return http("POST", f"{base}/v1/responses", timeout=60, headers={
            "Authorization": f"Bearer {key}", "User-Agent": "codex_cli_rs/0.162.1 (smoke)",
            "session-id": "smoke-test-0001",
        }, body={"model": "fake-tools", "stream": stream,
                 "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}]}).body

    def chat(text: str, stream: bool, api_key: str = key) -> str:
        return http("POST", f"{base}/v1/chat/completions", timeout=60,
                    headers={"Authorization": f"Bearer {api_key}"},
                    body={"model": "fake-tools", "stream": stream,
                          "messages": [{"role": "user", "content": text}]}).body

    def messages(text: str, stream: bool, ua: str) -> str:
        return http("POST", f"{base}/v1/messages", timeout=60, headers={
            "x-litellm-api-key": f"Bearer {key}", "anthropic-version": "2023-06-01", "User-Agent": ua,
        }, body={"model": "fake-tools", "max_tokens": 100, "stream": stream,
                 "messages": [{"role": "user", "content": text}]}).body

    check("入口から LiteLLM に届く", lambda: http_ok(f"{base}/health/liveliness"))
    for p in ["/openai/v1/models", "/anthropic/v1/messages", "/vertex_ai/x", "/bedrock/x"]:
        check(f"パススルー {p} は 403", lambda p=p: http("GET", base + p).status == 403)

    for stream in (False, True):
        s = str(stream).lower()
        check(f"Responses(stream={s}): 危険な操作は拒否に差し替わる",
              lambda: (b := responses(BAD, stream)) and "拒否: " in b and '"function_call"' not in b)
        check(f"Responses(stream={s}): 安全な操作は通る", lambda: '"function_call"' in responses(GOOD, stream))
        check(f"Chat(stream={s}): 危険な操作は拒否に差し替わる", lambda: "拒否: " in chat(BAD, stream))
        check(f"Chat(stream={s}): 安全な操作は通る", lambda: "tool_calls" in chat(GOOD, stream))
        check(f"Messages(stream={s}, 他のクライアント): 危険な操作は拒否",
              lambda: "拒否: " in messages(BAD, stream, "other-agent/1.0"))
        check(f"Messages(stream={s}, User-Agent なし): 審査される", lambda: "拒否: " in messages(BAD, stream, ""))
        check(f"Messages(stream={s}, Claude Code): 審査されない",
              lambda: "tool_use" in messages(BAD, stream, CLAUDE_UA))

    if env.has_users():
        check("ユーザー 1 の Codex 用キーで呼べる",
              lambda: http("POST", f"{base}/v1/chat/completions", timeout=30,
                           headers={"Authorization": f"Bearer {env.get('LAB_KEY_USER1_CODEX')}"},
                           body={"model": "fake-tools", "messages": [{"role": "user", "content": "hi"}]}).status == 200)
    if env.has_google():
        # マスターキーの持ち主は Google にサインインしないので、検証ユーザーのサインインの有無に関係なく 401 になる
        check("Gmail の MCP は未サインインなら 401", lambda: http("POST", f"{base}/gmail/mcp", headers={
            "x-litellm-api-key": f"Bearer {key}",
            "accept": "application/json, text/event-stream",
        }, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status == 401)

    passed, failed = results.count(True), results.count(False)
    sys.stderr.write("\n")
    if failed:
        raise LabError(f"疎通確認: {passed} 件 OK、{failed} 件 NG")
    ok(f"疎通確認: {passed} 件すべて OK")


def snapshot() -> list[str]:
    """変化を見る項目: Helm のリビジョン、Pod の UID と再起動回数、ConfigMap / Secret の resourceVersion、
    Deployment / StatefulSet の generation。"""
    lines: list[str] = []
    for r in json_output(helm("-n", NS, "list", "-o", "json"), default=[]) or []:
        lines.append(f"helm {r['name']} rev={r['revision']}")
    pods = json_output(kn("get", "pods", "-o", "json"), default={}) or {}
    for p in pods.get("items", []):
        if p["status"].get("phase") != "Running":
            continue
        restarts = sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
        lines.append(f"pod {p['metadata']['name']} uid={p['metadata']['uid']} restarts={restarts}")
    for kind in ("configmap,secret", "deploy,statefulset"):
        objs = json_output(kn("get", kind, "-o", "json"), default={}) or {}
        for o in objs.get("items", []):
            m = o["metadata"]
            if m["name"].startswith("sh.helm"):
                continue
            field = f"gen={m.get('generation')}" if o["kind"] in ("Deployment", "StatefulSet") \
                else f"rv={m['resourceVersion']}"
            lines.append(f"{o['kind']} {m['name']} {field}")
    return sorted(lines)


def idempotency() -> None:
    """./lab up を流した後にもう一度流し、2 回目で何も変わらないことを確かめる。"""
    step("test-idempotency 冪等性の試験")
    lab = [sys.executable, str(ROOT / "lab"), "up"]
    info("1 回目の ./lab up")
    run(lab)
    before = snapshot()
    info("2 回目の ./lab up")
    start = time.monotonic()
    run(lab)
    elapsed = int(time.monotonic() - start)
    after = snapshot()
    if before == after:
        ok(f"2 回目の ./lab up で何も変わりませんでした（{len(before)} 項目を比較、2 回目は {elapsed} 秒）")
        return
    warn("2 回目の ./lab up で変わったもの:")
    sys.stderr.writelines(l + "\n" for l in difflib.unified_diff(before, after, lineterm="", n=0))
    raise LabError("冪等ではありません")


def load(env: Env, vus: str, duration: str, scenario: str) -> None:
    """負荷試験（k6 を Docker で動かす）。相手は偽 LLM なので、利用枠を使わない。"""
    step("test-load 負荷試験")
    script = (ROOT / "tests" / "load" / "k6.js").read_text()
    rc = subprocess.run([
        "docker", "run", "--rm", "-i", "--add-host=host.docker.internal:host-gateway",
        "-e", f"BASE_URL=http://host.docker.internal:{env.http_port}", "-e", f"KEY={env.get('LITELLM_MASTER_KEY')}",
        "-e", f"VUS={vus}", "-e", f"DURATION={duration}", "-e", f"SCENARIO={scenario}",
        "grafana/k6:1.3.0", "run", "-",
    ], input=script, text=True).returncode
    info(f"試験中の Pod 数の変化は kubectl --context {KUBE_CONTEXT} -n {NS} get hpa,pods -w で見られます")
    if rc:
        raise LabError(f"負荷試験が失敗しました（終了コード {rc}）")


def review_log(since: str, raw: bool) -> None:
    """審査の判定ログ（LiteLLM の全 Pod の標準出力の "tool_call_review {...}" 行）を集計する。"""
    # 起動途中の Pod（自動で台数が増えた直後など）はログを取れないので飛ばす
    proc = run(kn("logs", "-l", "app.kubernetes.io/name=litellm,app.kubernetes.io/instance=litellm",
                  f"--since={since}", "--tail=-1", "--prefix=false", "--ignore-errors",
                  "--max-log-requests=10"), check=False, capture=True)
    records = []
    for line in proc.stdout.splitlines():
        if line.startswith("tool_call_review "):
            try:
                records.append(json.loads(line.removeprefix("tool_call_review ")))
            except ValueError:
                pass
    if not records:
        warn(f"直近 {since} の判定ログはありません")
        return
    if raw:
        for r in records:
            print(json.dumps(r, ensure_ascii=False))
        return
    print(summarize_reviews(records))


def summarize_reviews(records: list[dict]) -> str:
    reviews = [r for r in records if r.get("event") == "review"]
    passthrough = sum(1 for r in records if r.get("event") == "pass_through")
    denied = [r for r in reviews if not r.get("allowed")]

    def pct(a: int, b: int) -> str:
        return "-" if b == 0 else f"{a * 100 / b:.1f}%"

    totals = [r.get("timings_ms", {}).get("total") for r in reviews]
    totals = [t for t in totals if isinstance(t, (int, float))]
    out = [
        f"判定 {len(reviews)} 件（素通し {passthrough} 件）",
        f"  許可 {len(reviews) - len(denied)} 件 / 拒否 {len(denied)} 件 / "
        f"判定エラー {sum(1 for r in reviews if r.get('error') is not None)} 件",
        f"  二次判定まで進んだ割合: {pct(sum(1 for r in reviews if r.get('judge_used')), len(reviews))}",
        f"  判定時間（ミリ秒）: 平均 {int(sum(totals) / len(totals)) if totals else '-'} / "
        f"最大 {max(totals) if totals else '-'}",
        "",
        "クライアント別:",
    ]
    by_client = Counter(r.get("client") for r in reviews)
    denied_by_client = Counter(r.get("client") for r in denied)
    for client in sorted(by_client, key=str):
        out.append(f"  {client}: {by_client[client]} 件（拒否 {denied_by_client[client]} 件）")
    out += ["", "拒否の一覧（新しい順に最大 20 件）:"]
    for r in list(reversed(denied))[:20]:
        out.append(f"  {r.get('ts')} {r.get('client')} {','.join(r.get('tools') or [])}: {r.get('reason')}")
    return "\n".join(out)

