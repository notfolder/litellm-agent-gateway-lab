"""障害の試験の負荷の送り手（VPC の中の中継の VM で動かす。標準ライブラリだけ）。
標準入力の 1 行目で設定（JSON: url、key、rate）を受け取り、1 秒に rate 件、要求ごとに別のスレッドで送る。
結果は 1 件 1 行の JSON で標準出力に書く。標準入力が閉じたら新しい要求をやめ、送り中の要求を待って終わる。
変更設計書 .history/2026-11-05-障害試験の負荷をVPCの中から/ 1 章。"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

cfg = json.loads(sys.stdin.readline())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 中継の VM の環境変数のプロキシは使わない
lock = threading.Lock()
stop = threading.Event()
body = json.dumps({"model": "fake-tools", "messages": [{"role": "user", "content": "failover"}]}).encode()


def one(start: float) -> None:
    req = urllib.request.Request(f"{cfg['url']}/v1/chat/completions", method="POST", data=body,
                                 headers={"Authorization": f"Bearer {cfg['key']}", "content-type": "application/json"})
    status, call_id, detail = 0, "", ""
    sent = time.time()
    try:
        with opener.open(req, timeout=15) as resp:
            resp.read()
            status, call_id = resp.status, resp.headers.get("x-litellm-call-id", "")
    except urllib.error.HTTPError as e:
        status, call_id = e.code, (e.headers.get("x-litellm-call-id", "") if e.headers else "")
        try:
            detail = e.read()[:160].decode(errors="replace")
        except Exception:  # noqa: BLE001
            detail = ""
    except Exception as e:  # noqa: BLE001 接続できない・時間切れは 0
        detail = f"{type(e).__name__}: {str(e)[:120]}"
    line = json.dumps({"start": start, "sent": sent, "elapsed": round(time.time() - sent, 2), "status": status,
                       "call_id": call_id, "detail": detail})
    with lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def watch_stdin() -> None:
    sys.stdin.read()   # 閉じられるまで待つ
    stop.set()


threading.Thread(target=watch_stdin, daemon=True).start()
threads = []
interval = 1.0 / int(cfg.get("rate", 20))
nxt = time.monotonic()
while not stop.is_set():
    th = threading.Thread(target=one, args=(time.time(),), daemon=True)
    th.start()
    threads.append(th)
    nxt += interval
    time.sleep(max(0.0, nxt - time.monotonic()))
for th in threads:
    th.join(timeout=20)
