# aws-ec2 の配備と、台を止めたときの回復の遅れの直し
更新: 2026-11-06 17:02 JST
セッション: e2a02df0-f418-449f-9a21-285f82014ccd
ログ: ./session.md

## 概要
`aws-ec2` を初めて配備して試験し、台を止める場面の回復の遅れを 1 台だけの部品の冗長化と Traefik の設定で直して全 6 場面を目安 120 秒以内にした（PR #9、未マージ）。

## やったこと

- **依頼**: `LAB_TARGET=aws-ec2` の検証（2026-11-04〜）。途中から、目安を超えた `valkey-node`（回復 179.5 秒）の原因調査と直しに広がった。
- **配備で見つけた不具合の直し**: IAM のインラインポリシーの文字数の上限（カスタマー管理ポリシーに変更、文の統合）、同じホスト名に残った CNAME の自動削除、`entry-node` のロードバランサの健全の待ち（最長 180 秒）、Mac の `docker stop` の止まりの確認。
- **回復の遅れの原因と直し**（順に見つかった）:
  1. 試験の偽 LLM が 1 台だけ → 2 台（`k8s/fake-llm.yaml`）
  2. k3s の CoreDNS が 1 台だけ → 3 台（`stages.ensure_coredns`）
  3. Barman Cloud Plugin が 1 台だけ → VM は 2 台、`Recreate`、待ち方の変更（`render.barman_plugin_manifest`）
  4. 入口の Traefik が CPU の予約を持たず、CPU が混むと生存確認で再起動させられた → 予約 100m / 64Mi、生存確認 5 秒 × 6 回（`k8s/traefik-config-vm.yaml`）
- **試験の直し**: 要求ごとの記録（`failover.Sample`、`.build/failover-samples-*.json`）、managed の負荷を中継の VM から送る（`tests/failover-load/remote_load.py`、`failover.RemoteLoad`）。
- **最終の結果**（2026-11-05 18:13〜19:10、AWS）: pg-switchover・pg-pod・valkey-pod は失敗 0 件。pg-node 回復 90.9 秒、valkey-node 83.6 秒、entry-node 66.3 秒。監査ログの欠けは全場面 0 件、Traefik の再起動なし。
- **文書**: `.history/2026-11-05-*` の変更記録 3 つと、`.history/2026-10-31-内部LBとVM側の防御とZabbix/変更設計書.md` 15.2 節。`docs/spec.md`・`design.md`（21.10、21.13 節）・`vm_architecture.md`・`maintenance.md` に反映済み。
- **報告の訂正**: 最初の「LiteLLM の 4 台のうち 3 台が止めた台に偏った」は、終了処理中の Pod を数えた誤り。「ready の後の失敗は Mac の SSH の転送」という推定も誤りで、負荷を VPC の中に移して Traefik の再起動が原因と分かった。ユーザーに「何度もやり直していますが、なぜですか？」と問われ、最初の観測が足りなかったと説明した。

**環境の状態**: AWS の環境は 2026-11-05 19:10 に `./lab down` で消し、API で有料の資源 0 件を確認した。`.env` は `LAB_TARGET=aws-ec2` と `LAB_ZABBIX_LOCAL_PORT=18090` のまま（Mac で動かすときは `LAB_TARGET` を戻す）。

**意図的に残したもの**: metrics-server は 1 台のまま。`test-judge` は審査官の API キーの上限のため AWS で未確認（再実行は見送り）。Mac の k3d と `gcp-vm` では CoreDNS 3 台などの変更後の配備を流していない。

## 決定事項

- CoreDNS は `./lab up` のたびに同梱のものを 3 台にそろえる（自前で入れない）。
- Barman Cloud Plugin は VM で 2 台、止まった台からは 30 秒で移す。Mac は 1 台のまま。
- LiteLLM の readiness は DB を見るまま。DB に依存させると切り替えの間は覚えていないキーが 401 になり、クライアントが再試行しないため。PostgreSQL の切り替えの間の 503 は受け入れる。
- 障害の試験の負荷は、managed では中継の VM（VPC の中）から送る。判定の数え方は変えない。
- metrics-server の 2 台化はここでは行わず、バックログに残す。
- 原因調査は、経路と部品を洗い出し観測をそろえてから 1 回の配備でまとめて試す（Claude のメモリに記録）。クラウドで長く流す台本の最後に必ず片付けを入れる。

## 次にやること

1. プルリクエスト #9 をレビューしてマージする。
2. Mac の k3d と `gcp-vm` で変更後の `./lab up` と `test-failover` を確かめる。
3. Business Support+ を期限（2026-12-28）の前に Basic に戻す。

## バックログ

- [ ] プルリクエスト #9 をレビューしてマージする（重要 高）
- [ ] Mac の k3d と `gcp-vm` で変更後の `./lab up` と `test-failover` を確かめる（重要 低）
- [ ] metrics-server を 2 台にするか決める（重要 低）
- [ ] `worker-node` で、止める足す台に LiteLLM が載っていることを確かめてから止める（重要 低）
- [ ] Business Support+ を Basic に戻す（期限 2026-12-28、重要 高）
- [x] (2026-11-05) `aws-ec2` の配備と試験、`valkey-node` などの回復の遅れの直し
- [-] (2026-11-04) `aws-ec2` での `test-judge` の再実行 — 審査官の API キーの上限のため見送り
