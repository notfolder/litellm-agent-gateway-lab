# GCP（`LAB_TARGET=gcp-vm`）への配備と試験、VM 環境の共通化の実装
更新: 2026-10-30 22:35 JST
セッション: cc01e122-bd4d-40ab-ad74-ba0305719c21
ログ: ./session.md

## 概要
VM 3 台の環境を 2 クラウド共通の層に作り替えて GCP に配備し、疎通・冪等性・監査・障害 6 場面・審査官の試験まで通した（手戻り 19 件を修正）。GCP の環境は `./lab down` で消した。

## やったこと
- 2026-10-25〜30 の前半（このセッションの前半、圧縮済み）: `feature/aws-target` の取り込み、`docs/ec2_architecture.md`、GCP の実現性と CloudFormation 廃止の調査、5 回のヒアリングで変更仕様書と変更設計書（`.history/2026-10-30-VM環境の共通化とGCP追加/`）を確定し、EC2 版の作り替えと GCP の層（`labtool/clouds/{aws_ec2,gcp}.py`、`labtool/vm.py`、`vm/`）を実装（単体試験 69 本）。
- 2026-10-30 後半: GCP に実際に配備して試験。要った準備（Compute Engine の API の有効化、ADC のログイン、`.env` の秘密 3 つ、A レコード）を洗い出し、ユーザーの許可でブラウザの同意まで実行。
- 配備前の発見: 東京（asia-northeast1）で C4A が置けるゾーンは b と c だけ（ユーザー回答 1A: 3 台を b・c・b に置く。ゾーンの選び方を機種の有無で決めるように修正）。
- `./lab up` は 8 回目で全段階を通過。手戻り 7 件: 主系が浮動 IP を取ると公開 IP が静的 IP に入れ替わり SSH が届かない（公開 IP の引き直し、known_hosts を台の識別子で）、存在確認の `except Exception` が IAM の呼び方の誤りを隠した（`NotFound` だけに）、`.env` の Mac 用静的キーが VM の環境に混入、作った直後の SA で HMAC キーが 404、GCS の均一アクセスが registry の ACL を拒む（`images` だけ均一を切る）、GCS が `UploadPartCopy` 未実装（registry のコピーしきい値を 5 GiB に）、boto3 1.36+ のチェックサム送信を GCS が拒む（`when_required`）、保持の拒否コードが `RetentionPolicyNotMet`。
- 試験: smoke 75/75、冪等性 OK（65 項目。IAM バインディングの統合と記録の context の修正）、監査 31/32（残りは Gmail 未サインイン。修正: 試験の入口でのバケット接頭辞と S3 設定、HeadObject に版が無い→`x-goog-generation`、上書きに delete が要る→`objectUser`、LiteLLM の日次集計がローリング更新で取りこぼす→範囲で照合、整形の Job の OOM→2 GiB）、障害 6 場面すべて完走（監査ログの欠けは上限内。転送が切れる 2 件を修正）、審査官 255/288（Haiku 4.5 の差、14 事例）。
- 配備の約 2 時間後から外部の 1 つの IP が `/v1/models` に API キーの総当たりを毎秒 40 件（1 時間で 10.9 万件）送り続けた。ユーザー回答 A で入口の 80/443 を操作者の IP だけに絞った（`allow_web_from`、`LAB_WEB_ALLOW_CIDR`）。
- ユーザー指示「セットアップスクリプトで全て実行できるように」で、A レコードの作成（Cloudflare の API）と GCP の API の有効化を `./lab` に入れた。
- 障害試験の目安を GCP だけ広げた（entry-node 120 秒、台の停止 180 秒。回答 2A）。
- `./lab down` で GCP の環境を消した（回答 1B。バケットを空にする処理の引数の誤りと、保持ポリシーを外した直後の 403 を直してから。22:30 に VM・ディスク・バケット・静的キー・浮動 IP がすべて消えたことを確認）。
- 記録: 変更設計書 16 章（実装で分かったこと）と 16.1 節（試験の結果）、`~/.claude/estimate-log.md` に 2 行（文書 0.6 時間、実装 0.45 時間、配備と試験 3.3 時間＝補正後の見積もりどおり）。
- 意図的に残したもの: `docs/spec.md`・`docs/design.md`・`docs/maintenance.md`・`docs/ec2_architecture.md` への反映（配備と試験の後に行う決まり。次の作業）。`aws-ec2` の配備（vCPU 上限の解除待ち）。

## 決定事項
- 東京で 3 台を b・c・b に置く（1A）。3 ゾーンに分けたくなれば台湾 asia-east1。
- 入口（80/443）も 22 と同じく操作者の IP だけに開く（A）。理由: 公開の入口に API キーの総当たりが来て、監査の原本に消せない失敗の記録が増え続けるため。`.env` の `LAB_WEB_ALLOW_CIDR` で広げられる。
- A レコードの作成など、クラウドの API で行えることは `./lab` が行う（人の手作業を残さない）。
- 障害試験の目安は GCP だけ広げる（2A）。浮動 IP の付け替えは GCP の API の性質で縮められないため。
- GCP の環境は確認が済んだら消す（1B）。
- 審査官は Anthropic の API キーの Claude Haiku 4.5（Max のトークンを審査官に使うことは Anthropic が認めていない）。

## 次にやること
1. `docs/spec.md`・`docs/design.md`・`docs/maintenance.md` に VM 2 クラウドの構成を反映し、`docs/ec2_architecture.md` を書き直す（変更設計書 15 章）。
2. EC2 の vCPU 上限が解除されたら `aws-ec2` を配備して同じ試験を流す（AWS 固有の未確認: awscli の snap、netplan の 2 つ目の IP、Elastic IP）。
3. 審査官の指示文を Haiku 向けに詰めるか、事例の期待値を見直す（14 事例）。

## バックログ
- [x] (2026-10-30) GCP に配備して試験した（上記）
- [x] (2026-10-30) 障害の試験の目安を GCP だけ広げた
- [x] (2026-10-30) GCP の環境を `./lab down` で消した
- [ ] 配備と試験の後の文書の反映（`docs/spec.md`・`docs/design.md`・`docs/maintenance.md`・`docs/ec2_architecture.md`）（追加 2026-10-30、重要 中）
- [ ] `aws-ec2` の配備と試験（EC2 の vCPU 上限の解除後）（追加 2026-10-30、重要 高）
- [ ] 審査官を Claude Haiku 4.5 にしたときの不一致 14 事例の見直し（追加 2026-10-30、重要 低）
- [ ] k3s の版上げの試験（配備後。`.history/2026-10-26-k3s版上げ/`）（追加 2026-10-26）
- [ ] AMI / イメージを新しくする手順（追加 2026-10-26、重要 低）
