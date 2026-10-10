// 偽 LLM（fake-tools）に対する負荷試験。
// SCENARIO=chat: 文章だけの応答（ゲートウェイ自体の性能）
// SCENARIO=review: 毎回ツールコールが返り、審査（二次判定）が走る
import http from "k6/http";
import { check } from "k6";

export const options = {
  vus: Number(__ENV.VUS || 20),
  duration: __ENV.DURATION || "2m",
  thresholds: { http_req_failed: ["rate<0.01"] },
};

const base = __ENV.BASE_URL;
const headers = { Authorization: `Bearer ${__ENV.KEY}`, "content-type": "application/json" };
const text = __ENV.SCENARIO === "review"
  ? 'TOOL:shell:{"command":["pytest","-q"]}'
  : "hello";

export default function () {
  const res = http.post(`${base}/v1/chat/completions`, JSON.stringify({
    model: "fake-tools",
    messages: [{ role: "user", content: text }],
  }), { headers, timeout: "120s" });
  check(res, { "status 200": (r) => r.status === 200 });
}
