# BACKLOG-archive（litellm-agent-gateway-lab）

`BACKLOG.md` から移した完了 `[x]` と見送り `[-]`。消さずに移す。

## ツールコール審査

- [x] (2026-10-12) 文脈独立のツール引数監査（機密情報の持ち出し防止）を実装・試験し、docs に反映して main にマージ（PR #1）
- [x] (2026-10-13) 審査をすり抜けるローカル破壊的操作の対策。監査の判定を許可ベースに変更（取り返しのつかない操作は許可の明示がある場合だけ allow）、Jev の問いも直して main に push（`2ac468f`）。検証は `docs/guardrail-gaps.md`
- [x] (2026-10-13) 審査なしのキーを `./lab`（`LAB_NOREVIEW_KEYS`）で作る／消すようにして push（`a5600d3`）。手で発行した旧キーは削除済み
- [x] (2026-10-18) 機密データチェックで、名前から機密か分からないファイルを外へ送る操作の中身を確かめる（方針の差し込み方式・宛先の許可リスト・プログラムの実行の審査。PR #2）。経緯は `.history/2026-10-13-送信するファイルの中身の審査/`、`.history/2026-10-17-中身の審査の方式見直しと宛先の許可リスト/`、`.history/2026-10-18-0002-中身の審査の方式見直しと実装/`、`.history/2026-10-18-0315-PR2レビューと読み取り偽装の修正/`
- [x] (2026-10-18) PR #2 のレビューで見つけた残り（引用符の中の `cd`、中身付きの書き換え送信の回帰事例、`binary_artifact` の分岐、切れた読み取りの行き止まり）。クライアントの上限の数字で分岐する案は「特別扱いをしない」方針で退けた
- [x] (2026-10-18) Claude Code の auto mode と文脈あり審査の併用。設定 `LAB_CONTEXT_REVIEW_CLAUDE_CODE`（既定 false）。判定入力の抽出を text ブロックごとに直した。実機 165 回の結果は design 16.7
- [x] (2026-10-18) PC 操作系（メールの削除、ブラウザ・画面の操作、Windows）のツールコールの審査の点検と事例の追加（文脈あり 25 件・文脈独立 17 件。抜け 3 つを直した。PR #5）
- [x] (2026-10-18) 審査官の事例試験の揺れを調べて固定部分を直す（PR #3。41 事例 × 6 回の 246 件すべて期待どおり。経緯は design 16.6）
- [x] (2026-10-18) Claude Code・Codex・Copilot のエージェント組み込みの LLM 審査と設定方法を調査し、`docs/agent-builtin-review.md` にまとめて main に push。Codex にも Auto-review があるので Qiita 記事の下書きはいったん削除

## 冗長化と監査ログ

- [x] (2026-10-12) PostgreSQL 3 台（CloudNativePG）・Valkey と Sentinel・RustFS・監査ログ・検索アプリを実装し、試験を通して main に push
- [x] (2026-10-12) Gmail の MCP にサインインし直し、監査ログの試験 27/27（MCP の記録を確認）
- [x] (2026-10-12) 検索アプリのリダイレクト URI を Google Cloud に追加し、ブラウザでのサインインを確認

## AWS 構成

- [x] (2026-10-24) ElastiCache の `durability: sync` は `cache.t4g.micro` で使えなかった（Graviton `.large` 以上とクラスターモード有効が必須）。`min-replicas-to-write 1` に変更（ユーザー回答 1A）
- [x] (2026-10-24) 変更設計書 `.history/2026-10-24-AWS構成/変更設計書.md` を書く
- [x] (2026-10-24) 実装: `LAB_TARGET` の切り替え、`labtool/aws.py`、`cdk/`、S3 の資格情報チェーンとバケット名の接頭辞、ESO、ALB の Ingress、Bedrock のモデル登録、障害試験の AWS の場面。単体テスト（labtool 52、監査 19、監査ログ 12）と CDK の synth が通り、Mac の `./lab up` を流し直して確認

## VM 3 台構成（`LAB_TARGET=aws-ec2` / `gcp-vm`）

- [x] (2026-10-25) 4 回目のヒアリング（1A 無料の IAM・CloudFormation・Session Manager と従量の Bedrock は使ってよい、2 クラスタの中に registry を立てて保存先を S3 に、3A `chatgpt/` を置かない、4A `t4g.large` × 3）で前提を確定し 0.2 に移した
- [x] (2026-10-25) 実装: `labtool/cfn.py`（4 スタックのテンプレートを JSON で組む）、`cfn/node-init.sh`（k3s の分裂しない起こし方、keepalived、registry のミラー）、`labtool/ec2.py`（配備、SSM の Run Command とポート転送、kubeconfig、静的キーの受け渡し、EC2 の停止、削除）、`stages.py` の `aws-ec2` 分岐（registry、オペレーター replica 2、Let's Encrypt の入口）、`render.py`（Bedrock の静的キー、TLS の経路の写し、registry、`pg_backup_values`）、`objectstore.py`（利用者ごとの検査を両環境で共通に）、`failover.py`（`eip-node`、EC2 の停止）、`ops.py`、`lab`、`k8s/postgres-cnpg.yaml`、`k8s/traefik-config-ec2.yaml`、`.env.example`、README。単体試験 65 本（12 本追加）、cfn-lint と `validate-template` が通る。配備は未実施（ユーザー指示: 実装だけ）
- [x] (2026-10-26) k3s のマイナー版の無停止の版上げを調査（構成は耐えられるが手順が無く、user data の版変更で 3 台が同時再起動する。AMI の「最新」参照で配備後の `./lab up` が 3 台を置き換える不具合も発見）。ユーザー回答 1A（AMI の固定を今直す）・2C（手順を全部今書く）で実装: `AmiId` を固定 ID で受ける `resolve_ami`、k3s の版を SSM のパラメータに、`node-init.sh` の引数を `config.yaml` に、`./lab upgrade-k8s <版> [--measure]`（1 台ずつ、Elastic IP の台は最後、転送の張り直し、etcd のスナップショット、マイナー版 1 つずつ）、`./lab cluster` の版の食い違いの検知。LiteLLM の PDB は親ブランチ `feature/aws-target` の Helm チャートの値（`maxUnavailable: 1`）に寄せた（2 つ付くと drain が止まる）。単体試験 67 本、cfn-lint、`validate-template` が通る。変更仕様書・変更設計書は `.history/2026-10-26-k3s版上げ/`
- [x] (2026-10-30) GCP（`LAB_TARGET=gcp-vm`）に配備して試験した: `up` は 8 回目で全段階を通過（手戻り 7 件: 主系の公開 IP の入れ替わり、存在確認の except、`.env` の静的キーの混入、HMAC の反映遅れ、GCS の ACL と `UploadPartCopy`、boto3 のチェックサム、保持の拒否コード）。smoke 75/75、冪等性 OK（65 項目）、監査 31/32（残りは Gmail 未サインイン）、障害の試験 6 場面（監査ログの欠けは上限内。台を止める 3 場面は GCP の浮動 IP の付け替えで 80〜150 秒）、審査官 255/288。結果は変更設計書 16 章・16.1 節
- [x] (2026-10-30) GCP の環境を `./lab down` で消した（ユーザーの回答 1B。バケットの空にする処理の引数の誤りを直してから）
- [x] (2026-10-30) 障害の試験の目安を GCP だけ広げた（entry-node 120 秒、台の停止 180 秒。`failover.limit_for`。ユーザーの回答 2A）
- [x] (2026-10-25) 2 台案 → 3 台案 → CloudFormation のみ、の 3 回のヒアリングで決定（1A 3 台、2B Elastic IP ＋ keepalived ＋ Let's Encrypt、3B 静的キー、4A `.env`、5A 3 つ目の環境、6A 文書を一度に、基盤は素の CloudFormation）。変更仕様書と変更設計書を書いた
- [x] (2026-10-30) 変更仕様書（EC2 版）の「13. 前提」の 2 つ（浮動 IP は `./lab down` で消すのが既定、保持と SSO は EKS 版と同じ）は、VM 共通化の仕様書の 4 回目の回答（1A）で確定し決定事項に移した
- [-] (2026-10-30) EC2 版の配備と試験の項目（2026-10-25） — 基盤が CloudFormation から API の直接呼び出しに変わったので、「`aws-ec2` の配備と試験（EC2 の vCPU 上限の解除後）」の項目に統合
- [-] (2026-10-30) EC2 版の文書の反映の項目（2026-10-25） — 「配備と試験の後の文書の反映（VM 2 クラウド）」の項目に統合（k3s 版上げの `docs/maintenance.md` も含める）
- [x] (2026-10-31) 配備と試験の後の文書の反映: `docs/spec.md`・`docs/design.md`・`docs/maintenance.md` に VM 2 クラウドの構成（入口の送信元の制限、A レコードの自動化、整形のメモリ、GCS の違い）と k3s の版上げ（`.history/2026-10-26-k3s版上げ/`）を反映し、`docs/ec2_architecture.md` を VM 2 クラウドの構成図に書き直す（変更設計書 15 章）（追加 2026-10-30、重要 中） → docs/spec.md 12 章、docs/design.md 21 章、docs/maintenance.md 5 章、docs/vm_architecture.md
