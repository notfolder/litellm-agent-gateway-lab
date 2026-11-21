# ECS 構成（aws-ecs）の検討・実装・AWS での試験と全削除
更新: 2026-11-22 08:35 JST
セッション: 3f02c070-5992-40cc-b43c-79718ad92467
ログ: ./session.md

## 概要
ECS（Fargate）で本番に使える構成を 7 回のヒアリングで決め、1 ファイルの構築スクリプトと ./lab の試験の分岐を作り、AWS で managed と provided を配備して試験し、全部消した（ブランチ feature/aws-ecs）。

## やったこと
- ヒアリング（7 回）で構成を決めた。変更仕様書・変更設計書は `.history/2026-11-21-ECS構成/`（決定は変更仕様書 0.2 節、結果は変更設計書 15 章）
- 実装
  - `ecs/ecs-lab.sh`（bash 3.2 で動く 1 ファイル。CloudFormation のテンプレート、Valkey・Sentinel・PgBouncer の起動スクリプト、監視の台のユーザーデータ、Zabbix の取り込みの定義を中に持つ。up / down / status / smoke / forward / zabbix）
  - `labtool/ecs.py`（ECS の試験の部品）、`labtool` 9 ファイルの分岐（`LAB_TARGET=aws-ecs`。test-smoke、test-audit、test-failover の 5 場面、新しい `./lab test-ecs`）
  - `labtool/tests/test_ecs.py`（1 ファイルと labtool の突き合わせ 17 本）
- AWS での試験（managed と、managed の VPC を「用意された VPC」に見立てた provided）
  - 構築は両方通り、2 回目は 63〜66 秒で変更 0 件。test-smoke 75/75、test-ecs 9/9、test-audit 31/32（NG は MCP の記録。Gmail のサインインが要る）、test-failover 5 場面すべて目安以内（RDS の切り替えだけ失敗 40 件・回復 19.3 秒、ほかは 0 件。監査ログの欠け 0）
  - 社内プロキシ: 中継の記録に、端末からの AWS の API と Cloudflare、VPC の中からの Ubuntu・Docker Hub・Zabbix・Anthropic が現れた。provided の監視の台（private のサブネットの ASG）は中継越しに初期化できた
  - Zabbix: AWS 用の公式テンプレートが role_base で値を取れた（そのため監視の台は IMDSv1 を許す）。2 つの環境を 1 つのサーバーに載せる形も確かめた
  - 配備で見つけた不具合 20 件を、その場で直して確かめた（一覧は変更設計書 15.1 節）
- 全削除: provided、managed の順に down。有料の物は 0 件（ACM の証明書だけ無料なので残した）
- 端末: Session Manager のプラグインは sudo が要り入れられなかった（試験は中継を通すので要らない。`forward` だけが要る）
- `.env` に `LAB_ECS_*` と `LAB_ZABBIX_ADMIN_PASSWORD` が足された。`LAB_TARGET` は `aws-ec2` に戻した
- 意図的に残したもの: ACM の証明書（litellm-ecs / litellm-ecp。無料）と、その DNS 検証の CNAME

## 決定事項
- 本番で使える構成でないと検証の意味がない（機能を落とす案は取らない）
- 前提は本番寄り（内部 ALB だけ、社内プロキシの中）。使う部品は RDS・EFS・S3・ECS を中心に、外せない物（ECR、VPC エンドポイント、ALB、CloudWatch、IAM、Scheduler、Cloud Map）だけ
- Fargate（ARM64）、LiteLLM の設定は S3、秘密の値は S3 の環境変数のファイル、Valkey と Sentinel は別々の 6 サービス、PgBouncer 2 タスク、RDS はマルチ AZ の DB インスタンス、バケットは EKS 構成と同じ 3 つ
- 構築スクリプトは bash の 1 ファイル。試験は ./lab に置き、1 ファイルには疎通の確認だけ
- 監視は既存の Zabbix に登録。proxy は EC2（Fargate のタスクロールでは Zabbix の AWS 用テンプレートが認証できないため）。検証では Zabbix 一式・中継・踏み台も同じ EC2。登録は configuration.import
- 試験を全部通してから消すまでが作業。未了の試験があるのに down を提案しない（今回、審査官の試験と MCP の記録が残ったまま down を流そうとして指摘された）
- 今回は審査官の事例試験（test-judge）と MCP の記録は流さない。最後は「現時点で全て消す」の指示で消した

## 次にやること
- ECS 構成でまだ流していない試験: `test-load`（負荷で LiteLLM のタスクが自動で増える。k6 を中継越しに動かす作りが要る）、`test-restore` の ECS 構成向けの作り（RDS の任意の時点への復元）
- 必要になったら `test-judge`（審査官のキーは 2026-12-01 まで利用の上限）と MCP の記録
- 試験が済んだら `docs/spec.md` と `docs/design.md` に反映し、プルリクエストにする

## バックログ
- [ ] ECS 構成: 実装・AWS での試験・全削除は 2026-11-22 に済んだ。残りは test-load、test-restore の作り、（必要なら）test-judge と MCP の記録、docs への反映とプルリクエスト（BACKLOG.md の「ECS 構成」）
