#!/usr/bin/env bash
# 審査の判定ログ（LiteLLM の全 Pod の標準出力の "tool_call_review {...}" 行）を集計する。
# 使い方: summarize-log.sh [--since 1h] [--raw]
source "$(dirname "$0")/../../scripts/lib.sh"
since="1h"; raw=false
while (($#)); do
  case "$1" in
    --since) since="$2"; shift 2 ;;
    --raw) raw=true; shift ;;
    *) die "不明な引数: $1" ;;
  esac
done

# 起動途中の Pod（自動で台数が増えた直後など）はログを取れないので飛ばす
logs="$( { kn logs -l app.kubernetes.io/name=litellm,app.kubernetes.io/instance=litellm --since="$since" --tail=-1 \
  --prefix=false --ignore-errors --max-log-requests=10 2>/dev/null || true; } | sed -n 's/^tool_call_review //p')"
if [[ -z "$logs" ]]; then
  warn "直近 ${since} の判定ログはありません"
  exit 0
fi
if $raw; then echo "$logs"; exit 0; fi

echo "$logs" | jq -s -r '
  def pct(a; b): if b == 0 then "-" else ((a * 1000 / b | floor) / 10 | tostring) + "%" end;
  (map(select(.event == "review"))) as $r
  | (map(select(.event == "pass_through")) | length) as $p
  | "判定 \($r | length) 件（素通し \($p) 件）",
    "  許可 \($r | map(select(.allowed)) | length) 件 / 拒否 \($r | map(select(.allowed | not)) | length) 件 / 判定エラー \($r | map(select(.error != null)) | length) 件",
    "  二次判定まで進んだ割合: \(pct($r | map(select(.judge_used)) | length; $r | length))",
    "  判定時間（ミリ秒）: 平均 \(if ($r|length)>0 then ($r | map(.timings_ms.total) | add / length | floor) else "-" end) / 最大 \($r | map(.timings_ms.total) | max // "-")",
    "",
    "クライアント別:",
    ($r | group_by(.client)[] | "  \(.[0].client): \(length) 件（拒否 \(map(select(.allowed | not)) | length) 件）"),
    "",
    "拒否の一覧（新しい順に最大 20 件）:",
    ($r | map(select(.allowed | not)) | reverse | .[:20][] | "  \(.ts) \(.client) \(.tools | join(",")): \(.reason)")'
