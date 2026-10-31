# 内部ロードバランサへの入口の変更、VM 側の防御、踏み台、Zabbix（GCP で配備と試験、片付けまで）
更新: 2026-10-31 13:10 JST
セッション: 6fdf13be-d0bd-4940-9a69-2889f6649720
ログ: ./session.md
ブランチ: `feature/aws-ec2-pair`（ワークツリー `~/orca/workspaces/litellm-agent-gateway-lab/feature-aws-target-2`）

## 概要
入口を公開の浮動 IP（keepalived）から内部ロードバランサに変え、VM 側の 3 層の守りと踏み台、Zabbix（サーバーの自動構築を含む）を足して GCP で全試験と版上げの計測を通し、資源を全部消した。

## やったこと
- 前半（前のセッションの続き）: 既存資源からの構築・1 バケット・出口プロキシ・VM 上のビルドの GCP 配備の最終報告。障害時・保守時の切断時間の整理の報告。
- ヒアリング（決定事項は下）を経て、変更仕様書・変更設計書 `.history/2026-10-31-内部LBとVM側の防御とZabbix/` を作成。
- 実装（単体テスト 100 本）: `labtool/clouds/*.py`（内部 NLB / 内部パススルー NLB、VM の権限とフックの廃止）、`vm/node-init.sh`（nftables、Zabbix agent 2、keepalived の廃止）、`vm/proxy-init.sh`（踏み台の nftables、Zabbix サーバー）、`labtool/vm.py`（転送 3 本、provided の SSH は踏み台経由）、`labtool/zabbix.py`（新規）、`render.admin_only_manifest`、`k8s/network-policies.yaml`、`k8s/traefik-config-vm.yaml`（hostPort）、`./lab forward` / `zabbix`。
- GCP で `./lab up` 13 回目に全段階通過。守りの確認、Zabbix 6 ホスト、smoke 75/75、idempotency 変化なし、audit 31/32、failover 5/6（entry-node 131.5 秒）、provided の down/up、`upgrade-k8s v1.36.5 --measure`（235 秒、失敗 593/4680 件、幅 88.3 秒）、片付けで有料の物は 0。
- 配備で分かったこと 28 件を変更設計書 15 章に。主なもの: ServiceLB は送信元を書き換える（hostPort が要る）、k3s 1.36 の Traefik チャート 40 は Service の鍵が変わる、cert-manager の DNS-01 の伝搬確認はクラウドの再帰リゾルバに向ける、Zabbix の arm64 リポジトリは別、provided では構築ツールの SSH も踏み台経由。
- 既存文書の反映: `docs/spec.md` 12 章、`docs/design.md` 21 章（21.10 結果、21.11 Zabbix）、`docs/vm_architecture.md`（書き直し）、`docs/maintenance.md` 5 章、README、`.env.example`、バックログ。`~/.claude/estimate-log.md` に 1 行。
- 意図的に残したもの: `core.K3S_IMAGE` を v1.36.5-k3s1 に上げた（Mac の k3d も同じ版になる。k3d での動作は未確認）。`entry-node` の目安は 60 秒のまま（確認待ち）。

- 追記（同日 13:10 以降、同じセッション）: 閲覧と管理操作の記録の保持を 90 日に。バケットの静的キーを 5 つから 2 つ（litellm-writer、lab-app）に変えて GCP で配備・試験・片付け（`.history/2026-10-31-静的キーを2つに/`）。担当部署への依頼事項を `docs/provider-requests.md` に。`./lab provider-requirements` を追加。

## 決定事項
- 入口の台の切り替え（中継経由で 140〜227 秒）は受け入れ、中継なしの再計測はしない（本番は AWS で、計測の意味が薄い）。
- 本番では利用者は VPC の内側から LiteLLM を呼ぶ。Zscaler は外向きだけ。公開の入口は要らない。
- 入口は内部ロードバランサ（AWS: 内部 NLB、GCP: 内部パススルー NLB）。理由: クラウドの権限は別部署が持ち、VM が実行時にクラウドの API を呼ぶ方式（keepalived ＋ IP の付け替え）は VM に IAM の権限を頼む依頼が残るため。層をまたぐ操作を無くす。AWS の 3 台は別 AZ のまま。
- クラウドのファイアウォールは無い前提で VM 側で守る。管理画面と DB への接続元は踏み台（別部署が用意する 1 台。検証では中継の VM）だけ。Mac のクライアントも踏み台経由の転送口を使う（別名 `LAB_LOCAL_HOSTNAME`）。
- 監視は Zabbix。agent 2 と Kubernetes のテンプレートまで今回に含め、Zabbix サーバーも `./lab` が自動で作る。
- 報告は決め事どおり（終了見込みは時計の時刻、進捗は項目ごとの表、確認は末尾）。日本語で。

## 次にやること
- `entry-node` の目安（60 秒）の扱いをユーザーの答えに合わせて直す（A: 台の停止と同じ 120/180 秒、B: primary を先に移してから止める手順、C: そのまま）。
- バックログの整理（未完 36 件で上限 30 を超えている。古い項目の続行・見送りの確認）。
- AWS（`aws-ec2`）の配備（vCPU 上限の解除後）。

## バックログ
- [ ] `entry-node` の目安の決定と反映（追加 2026-10-31、重要 高）
- [ ] k3s の版上げの試験の残り（パッチの更新、飛ばす・下げる指示、途中で止めて再実行、1 台の作り直し。GCP の v1.35.5 → v1.36.5 の計測は済み）（追加 2026-10-26、重要 高）
- [ ] Mac の k3d を `K3S_IMAGE` v1.36.5 で動かして試験が通ることを確かめる（追加 2026-10-31、重要 低）
- [ ] `aws-ec2` の配備と試験（内部 NLB、ロール無しの EC2 を含む）（追加 2026-10-30、重要 高）
- [ ] 既存資源（provided）を本物の用意する側の物で（ロードバランサ、踏み台、`LAB_VM_CIDR`）（追加 2026-10-31、重要 中）
- [ ] `./lab down --all` の実機確認（ロードバランサをたどる経路を含む）（追加 2026-10-31、重要 中）
- [ ] Zscaler の実物での確認（DNS-01 の伝搬確認、Zabbix のリポジトリと Helm の取得を含む）（追加 2026-10-31、重要 中）
- [ ] Zabbix に LiteLLM と PostgreSQL の内部指標を取り込む（追加 2026-10-31、重要 中）
- [ ] 本物の踏み台での確認（SSH の経路が別のとき）（追加 2026-10-31、重要 中）
- [ ] AWS での entry-node と版上げの切断時間の計測（追加 2026-10-31、重要 中）
- [ ] Zabbix のノードのテンプレートの扱い（追加 2026-10-31、重要 低）
- [ ] amd64 の VM での検証（追加 2026-10-31、重要 低）
