# litellm-agent-gateway-lab

LiteLLM を AI エージェント向けゲートウェイとして使うための検証リポジトリです。

- **Copilot エージェント連携**: LiteLLM の MCP ゲートウェイとユーザーごとの OAuth で、Microsoft 365 Copilot（Copilot Studio のエージェント）を Claude Code や Codex から呼び出す（検証は Gmail で代用）
- **ツールコール審査**: エージェントのツールコールを、実行の前に LiteLLM のガードレールで審査する。2 つの審査を並列に掛け、どちらかが止めたら止める
  - 文脈あり審査: Claude Code の auto mode を参考にした審査。会話を見て、取り返しのつかない操作を実行の許可が会話に無ければ止める（Codex など Claude Code 以外。`.env` の `LAB_CONTEXT_REVIEW_CLAUDE_CODE=true` で Claude Code にも掛けられる。auto mode を外した運用の守り）
  - 文脈独立審査: 会話を見ず、ツール名・引数・ツールの定義だけで、機密情報の持ち出しを止める（Claude Code にも掛ける）。送信の宛先を許可リスト（既定はプライベートアドレス）で絞り、送るファイルと名指しのプログラムは、モデル自身にゲートウェイ指定のコマンドで読ませて中身を審査する
- **スケール検証**: Mac 1 台の Kubernetes（OrbStack + k3d）で、複数レプリカ構成の LiteLLM を検証する
- **冗長化と監査ログ**: PostgreSQL（CloudNativePG、3 台）と Valkey（Sentinel）をマネージドサービスを使わずに冗長化し、LLM への入出力を改ざんできない形で 90 日残す。利用者ごとの利用料（概算と按分）と入出力の履歴を検索するアプリ（`/lab-audit/`）付き

全体の構成図は [docs/aws_architecture.md](docs/aws_architecture.md)（Mac と EKS）と [docs/vm_architecture.md](docs/vm_architecture.md)（VM 3 台とバケット 1 つ。AWS と GCP）、仕様は [docs/spec.md](docs/spec.md)、詳細設計は [docs/design.md](docs/design.md) を参照してください。各エージェントが自分の中に持つ LLM 審査（Claude Code の auto mode、Codex の Auto-review、Copilot Studio のリアルタイム保護）と設定方法の調査は [docs/agent-builtin-review.md](docs/agent-builtin-review.md) にあります。変更ごとの経緯（変更仕様書と変更設計書）は `.history/<日付>-<目的>/` にあります。

## 構築

前提: macOS（Apple Silicon）、[OrbStack](https://orbstack.dev/)（メモリの上限 12 GB）、Homebrew。

```bash
brew install uv k3d kubectl helm kubectl-cnpg
./lab up            # 何度実行しても同じ結果になる。初回は .env を .env.example から作る
./lab test-smoke    # 疎通確認（偽 LLM を使うので、利用枠を使わない）
```

構築ツール `./lab` は Python で書いてあり、uv が Python 3.12 以上と依存を自動で用意します（`./lab -h` でコマンドの一覧）。

手作業の準備（なくても構築は進み、該当する機能だけが無効になる）:

| 準備 | 手順 | 有効になるもの |
|---|---|---|
| Google の OAuth クライアント | [docs/setup-google.md](docs/setup-google.md) | 管理画面の SSO、Gmail の MCP、検索アプリ（`/lab-audit/`）のサインイン |
| TypeSafe の API キー | [console.typesafe.ai](https://console.typesafe.ai) の Billing でクレジットを追加してから、API Keys でキーを作り、`.env` の `TYPESAFE_API_KEY` に入れる | 審査の一次判定（Jev） |
| ChatGPT Plus のログイン | 先に ChatGPT の設定 →「セキュリティ」で、デバイスコードによるサインイン（Codex など）を有効にする。その後 `./lab chatgpt-login`、`./lab up` | Codex 用のモデル、2 つの審査の審査官（無いあいだは試験用の偽 LLM） |

## AWS で動かす（`LAB_TARGET=aws`）

同じコードを AWS（EKS Auto Mode、Aurora PostgreSQL、ElastiCache for Valkey、S3、Bedrock）でも動かせます。Mac の構成を置き換えるのではなく、両方を維持します（審査・監査・検索アプリの層は環境を知らず、違いは `./lab` の分岐と `cdk/` だけ。経緯は `.history/2026-10-24-AWS構成/`）。

```bash
brew install awscli            # kubeconfig の発行に要る。CDK は npx（Node）で動く
aws configure sso              # プロファイルを作り、aws sso login --profile <名前>
# .env: LAB_TARGET=aws、LAB_AWS_PROFILE=<名前>、LAB_AWS_HOSTNAME=<HTTPS のホスト名>
./lab up                       # CDK の配備 → kubeconfig → イメージを ECR へ → Secrets Manager → LiteLLM → ALB
```

人が行うこと: Bedrock のモデルアクセス（Anthropic の Claude）を有効にする、ACM の検証用 CNAME（配備の途中で表示）と、ホスト名 → ALB の CNAME（`./lab litellm` が表示）を手持ちの DNS に足す。`./lab down` は AWS の有料リソース（EKS、ALB、Aurora、ElastiCache、EFS、NAT、S3 の監査ログ、秘密情報）をすべて消します。残るのは無料のもの（ACM の証明書、Identity Center、DNS の CNAME、CDK の bootstrap）だけです。

## VM 3 台とオブジェクトストレージだけで動かす（`LAB_TARGET=aws-ec2` / `gcp-vm`）

マネージドサービスを使わず、クラウドの部品を計算資源とオブジェクトストレージに限って同じ構成を動かす環境です。AWS は EC2 3 台（別 AZ）と S3 のバケット 1 つ、GCP は Compute Engine 3 台（別ゾーン）と Cloud Storage のバケット 1 つ。どちらも Ubuntu 24.04 LTS の k3s（server 3 台）で、Kubernetes の中は Mac の k3d と同じ（CloudNativePG 3 台、Valkey と Sentinel、Traefik）、RustFS だけがクラウドのバケットのディレクトリ（`audit-raw/` など）に替わり、原本と閲覧記録にはオブジェクト単位の保持を付けます。クラウドの違いは `labtool/clouds/` に閉じ、VM の中（SSH で初期化）と Kubernetes の中は 1 つのコードです。1 台が落ちても処理が続きます。構成図は [docs/vm_architecture.md](docs/vm_architecture.md)、仕様は docs/spec.md 12 章、設計は docs/design.md 21 章（経緯は `.history/2026-10-25-EC2三台構成/`、`.history/2026-10-30-VM環境の共通化とGCP追加/`、`.history/2026-10-30-既存資源からの構築と出口プロキシ/`。GCP で配備と試験済み、AWS は EC2 の vCPU 上限の解除待ち）。

基盤の持ち方は 2 つあります（`.env` の `LAB_INFRA`）。

- `managed`（既定、検証用）: `./lab up` がクラウドの API でネットワーク・バケット・静的キー・VM 3 台・内部ロードバランサ・中継（踏み台、Zabbix サーバー）の VM を作り、識別子を `.env` に書いてから組み立てます。`./lab down` で有料のものがすべて消えます
- `provided`（本番用）: 既存の VM 3 台、バケット 1 つ、内部ロードバランサ、踏み台を使います。用意する側（VM、ネットワーク、ロードバランサ、踏み台、バケット、役割ごとの静的キー。VM にクラウドの権限は不要。前提は docs/spec.md 12.2）から受け取った識別子と静的キーを `.env` に書き、`./lab up` はその上に組み立てるだけで、ネットワークやバケットの設定は変えません。`./lab down` は k3s を外して保持の無いディレクトリを空にし、`./lab down --all` で VM・ロードバランサ・バケットまで消します

```bash
brew install kubectl-cnpg awscli            # GCP は brew install --cask google-cloud-sdk、gcloud auth application-default login
# .env: LAB_TARGET=aws-ec2 か gcp-vm、LAB_HOSTNAME、LAB_CLOUDFLARE_API_TOKEN（Zone / DNS / Edit）、LAB_ACME_EMAIL、
#       LAB_JUDGE_ANTHROPIC_API_KEY（審査官）。AWS は LAB_AWS_PROFILE、GCP は LAB_GCP_PROJECT。provided は LAB_VM_* の識別子と静的キー
./lab up   # managed: 基盤を作る → .env に書く → SSH で k3s を起こす → イメージを VM でビルドして中の registry へ → LiteLLM → Let's Encrypt
```

本番の VM は Zscaler（外向きの通信を独自 CA で中継する装置）の中にある前提です。`.env` の `LAB_EGRESS_PROXY`（中継の URL）と `LAB_EGRESS_CA_FILE`（CA）を書くと、VM・k3s・外へ出る全 Pod がその中継を通り CA を信頼します。`LAB_EGRESS_PROXY_MAC` で `./lab` 自身も中継を通せます。検証では `LAB_EGRESS_PROXY_VM=true` で 4 台目の VM に中継（mitmproxy）を作り、3 台の直接の外向きを遮断した状態で全試験を通しました（`./lab proxy-log` で中継の記録を宛先ごとに数えられます）。

自作イメージ 2 つ（LiteLLM、監査アプリ）は Mac ではなく k3s の台の Docker でビルドします（アーキテクチャは VM のもの。Mac に Docker は要りません）。入口は内部ロードバランサ（AWS: 内部 NLB、GCP: 内部パススルー NLB）の固定の内部 IP で、利用者は VPC の内側からホスト名で呼びます。A レコード（ホスト名 → その IP、Mac 用の別名 `LAB_LOCAL_HOSTNAME` → 127.0.0.1）は `./lab litellm` が Cloudflare の API で作り、HTTPS は cert-manager が Let's Encrypt（DNS-01）で取ります。守りはクラウドのファイアウォールでなく各 VM の nftables、Traefik の送信元制限（管理画面と検索アプリは踏み台からだけ）、Kubernetes の NetworkPolicy です。構築ツールは SSH で VM に届き、HTTPS は踏み台経由の転送口（`https://<別名>:18443`。`./lab forward`）で入口に届きます。監視は Zabbix（agent 2 を全 VM に、公式 Helm チャートを k3s に。検証ではサーバーも踏み台の VM に立て、`./lab zabbix` が API で登録）。Claude Code のモデルは Mac と同じ Claude Max のトークンの転送、2 つの審査官は Anthropic の API キーの Claude Haiku 4.5 です。障害の試験は Mac と同じ場面に `--scenario entry-node`（Traefik の載った台を止める）が加わります。Kubernetes（k3s）の版は `./lab upgrade-k8s <版>` が 1 台ずつ上げます（経緯は `.history/2026-10-26-k3s版上げ/`）。

## よく使うコマンド

| コマンド | 内容 |
|---|---|
| `./lab up` | 全段階を順に流す（段階ごとにも流せる。例: `./lab litellm`） |
| `./lab rebuild` | クラスタを作り直す（DB と監査ログは消える。`.env`、ChatGPT のトークン、イメージの置き場は残る） |
| `./lab status` | 状態の表示 |
| `./lab upgrade v1.x.y` | LiteLLM のローリング更新 |
| `./lab upgrade-k8s v1.x.y+k3s1 [--measure]` | VM 3 台の k3s（Kubernetes）を 1 台ずつ上げる（`aws-ec2` / `gcp-vm` だけ。マイナー版は 1 つずつ。`--measure` で上げている間の要求の失敗を測る） |
| `./lab test-unit` / `test-smoke` / `test-idempotency` / `test-load` | 試験 |
| `./lab test-judge [--context \| --isolated]` | 審査官の事例試験（本物の審査官を呼ぶので ChatGPT の利用枠を使う。指定なしは両方の審査） |
| `./lab test-failover [--pooler direct]` / `test-restore` | DB と Redis の障害の試験（EKS 版は `--scenario litellm-drain` / `aurora-failover` / `valkey-failover`、VM 3 台は Mac の場面 ＋ `entry-node`） / バックアップから任意の時点に戻す試験 |
| `./lab down [--all]` / `./lab proxy-log` / `./lab forward` / `./lab zabbix` | VM 3 台: `down` は managed なら全部消し、provided なら k3s を外す（`--all` で VM・ロードバランサ・バケットも） / 中継（出口プロキシ）の記録の集計 / SSH の転送 3 本を張る / Zabbix の登録 |
| `./lab test-audit` | 監査ログの試験（記録、整形、検索アプリ、精算、改ざんの防止、退避と再送） |
| `./lab audit-synth` / `audit-bench` / `audit-compact` | 検索の速さの試験用の合成データ / 速さの試験 / 監査ログの整形を手で流す |
| `./lab review-log` | 審査の判定ログの集計 |
| `http://localhost:8080/lab-audit/` | 利用料と入出力の履歴の検索アプリ（Google でサインイン） |
| `clients/claude-lab` / `clients/codex-lab` | 検証用の LiteLLM を通して Claude Code / Codex を起動（普段の設定は変えない） |

## ステータス

構築スクリプト、審査ガードレール（文脈あり審査と文脈独立審査）、DB と Redis の冗長化、監査ログ、検索アプリを実装し、試験済み（2026-10-12。結果は design 16.5）。文脈独立審査に、宛先の許可リストと中身の審査（送るファイル、動かすプログラム）を足し、試験済み（2026-10-17。結果は design 16.5）。

文脈独立審査の判定基準は `litellm/guardrails/policies/confidential.md`（社内の宛先などはプレースホルダー）。送信してよい宛先は `.env` の `LAB_ISOLATED_DESTINATIONS`（既定 `private`。GitHub への push やメールを使うなら足す）。変えたら `./lab litellm` で反映します。

## 注意

- 個人の検証環境での記録です。テナント ID やドメインなどはすべてプレースホルダーです
- Claude Max と ChatGPT Plus は個人の検証にだけ使い、エンドポイントを他人と共有しません（規約上の扱いは spec 8.2）
- **機密情報の審査（文脈独立審査）は、送るファイルと名指しのプログラムの中身は読みますが、木のツール（`cargo run`、`pytest`、`make` など）が動かす木の中身までは読みません。** 一時的なコードを木の中に置いて木のツールで動かす持ち出しは止められません（利用者が差分で見る前提）。読んだ中身は審査官のモデルに渡ります
- **既定の許可リストはプライベートアドレスだけ**なので、GitHub への push やメールの送信もゲートウェイ経由では止まります。使うなら `LAB_ISOLATED_DESTINATIONS` に足してください
- Claude Code、Codex、Microsoft 365 Copilot、LiteLLM は各社の製品・商標です。本リポジトリは各社と無関係です
