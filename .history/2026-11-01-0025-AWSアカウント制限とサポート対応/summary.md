# AWS 構成の実装と、AWS アカウントの制限の解除待ち
更新: 2026-11-01 00:40 JST
セッション: 5480596a-d511-489e-9ce0-6a985b4a42c6
ログ: ./session.md

## 概要
AWS 構成（`LAB_TARGET=aws`）を実装して配備を始めたが、アカウント単位の制限（計算資源の上限 0、Bedrock Error 002）で止まり、Business Support+ に加入して 3 件のサポートケースで解除を待っている。

## やったこと

### AWS 構成の実装（2026-10-24〜10-26）
- 依頼: 「前回の AWS 構成の検討をしたい」「自前の k8s 構成を置き換えず、両方で動くように維持し続けたい」。
- ヒアリングで決めて変更仕様書・変更設計書（`.history/2026-10-24-AWS構成/`）を書き、実装した: `LAB_TARGET` の切り替え、`labtool/aws.py`、`cdk/`（EKS Auto Mode、Aurora、ElastiCache Valkey、S3、Secrets Manager と ESO、ALB、ACM）、Bedrock のモデル登録、障害試験の AWS の場面。
- IAM Identity Center（東京、利用者と許可セット）と SSO プロファイル `litellm-lab` を用意。Cloudflare に ACM 検証の CNAME を追加。
- 初回の配備で cert・network・data の 3 スタックまで通り、EKS のノードが EC2 の vCPU 上限 0 で起動できなかった。
- ユーザーの指示「`./lab down` で消えない有料リソースは困る」により、`./lab down` で有料リソースをすべて消すように変更し、実機で確認（残るのは無料の ACM 証明書、CDK の bootstrap、Identity Center、Cloudflare の CNAME）。
- EKS の版を Mac の k3s と同じ 1.35 に、LiteLLM に PDB（maxUnavailable 1）。無停止の版上げの可否を `docs/maintenance.md` に記録。構成図 `docs/architecture.md`（`docs/aws_architecture.md`）を Mermaid で追加。

### AWS アカウントの制限への対応（2026-10-24〜11-01）
- 症状: EC2 のオンデマンド・スポットの vCPU 上限 0、Fargate 0、Lightsail 0、App Runner は作成拒否、SageMaker・CodeBuild・EMR Serverless も 0。Bedrock は全モデル・全リージョンで `Error 002: Access to Bedrock models is not allowed for this account`、Anthropic の利用目的フォーム（`PutUseCaseForModelAccess`）も not authorized。ElastiCache・RDS・S3・EKS の制御部分は使える。
- アカウントは 2018 年作成の休眠アカウント（`AWSServiceRoleForSupport` の作成日 2018-08-24、2026 年 10 月より前の利用料 0、カード期限切れ）を 10/24 に再開したもの。カードは 10/24 に差し替え済み。
- サポートケース:
  - 179284818700912（アカウントと請求、Bedrock の解除）: 10/24 起票、未割り当てのまま。10/29・10/30・10/31 に英語で追記。
  - 179285325800171（EC2 の 16 vCPU、Service Quotas の申請 `4a733396241d475f8b46f02a9a2973f8cLD9swzf`）: 10/24 に定型の受付応答のみ。分類は `account-verification`。10/29・10/31 に追記。
  - 179340373000600（技術ケース、Bedrock / models、重大度 Normal）: 10/31 08:42 起票。生成 AI の自動応答「アカウント単位の制限、既存 2 ケースで対処」→ 人の割り当てとエスカレーションを要求する返信（08:52）と、作成時期の訂正（08:58）を投稿。10/31 23:07 に「対応中」へ変わったが文面はまだ。
- 調査: re:Post の 2026 年の事例では、担当者が付くまで 1〜3 週間、「1 請求サイクルの利用実績を積んでから」で却下される例もある。有料プランで解除が早まった実績は見つからなかった。Developer サポートは 2025-12-02 に新規受付終了。
- 利用実績づくり: 計算資源は何も作れないので、ElastiCache Valkey cache.t4g.micro × 1（`litellm-lab-activity`、東京 1c、約 15 ドル/月）を 10/30 00:21 から稼働。既定 VPC を作り直し、サブネットグループ `litellm-lab-activity`（1c・1d）を作成。
- Business Support+ の期間限定トライアルに 10/31 に加入（ユーザー承認、ボタンは私が押した）。標準価格が先に請求され、2 か月分を自動返金。トライアルの終了表示は 12/31/2026、60 日を過ぎると月 29 ドルの通常課金。Support API が使えるようになり、ケースは `aws support describe-cases --region us-east-1 --include-communications` で読める。
- 誤り: 技術ケースへの返信に「account created in October 2026」と未確認で書いた。ユーザーの指摘で確かめて訂正を投稿した。

### main との取り込み（2026-11-01）
- 子ブランチ `feature/aws-ec2-pair`（VM 3 台構成）が PR #6 で main に入った。main をこのブランチにマージ（衝突なし）して push。main の単体テスト（labtool 81、監査 19、ガードレール 100。LiteLLM 1.104.2 を入れて全件）が通ることを確認。
- 取り込み済みのローカルブランチ `feature/aws-target-2` を削除。orca の作業ツリーと `feature/aws-ec2-pair` は、中で Claude のセッションが動いていたので残した。

## 決定事項
- AWS 構成は Mac の k3d 構成を置き換えず、両方で動かし続ける（2026-10-24）。
- `./lab down` は有料リソースをすべて消す（2026-10-25、ユーザー指示）。
- 利用実績づくりの予算は月 30 ドル程度まで（「実績のためだけに 100 ドルは大きい」）。置いたのは ElastiCache 1 台だけ。
- 費用がかからない範囲で Business Support+ を試す。加入は請求の初回決済を待たずに行う（ユーザー判断 1A）。
- 外部に送る文面に書く事実は、送る前に API などで裏を取る（作成時期の誤りから）。
- 待機は起動後 1〜2 分で失敗を確かめてから。確認の間隔は終了見込みに合わせ、30 分を超えない（2026-10-24、ユーザー指示）。

## 次にやること
- 3 件のケースの返答を Support API で読み、求められた情報を出す。返答が 11/7 ごろまで無ければ re:Post への投稿（下書きは session.md にある）か、Business Support+ のチャット・電話を検討する。
- 制限が解けたら: 実績用 ElastiCache とサブネットグループを消す → Anthropic の利用目的フォームを送り直す → `LAB_TARGET=aws` で `./lab up` → Cloudflare に ALB の CNAME（DNS のみ）→ 変更仕様書 7.2 の試験。
- 12 月初旬に 11 月分の明細で Business Support+ の請求と返金を確かめ、12/28 までに解約する（制限が先に解ければその時点で解約）。
- orca の作業ツリーで動いているセッションをユーザーが閉じたら、作業ツリーと `feature/aws-ec2-pair` を消す。

## バックログ
- [ ] AWS サポートの 3 ケースの返答に対応して、計算資源と Bedrock の制限を解除してもらう（追加 2026-11-01、重要 高）
- [ ] Business Support+ のトライアルを解約する（追加 2026-11-01、期限 2026-12-28、重要 高）
- [ ] 11 月分の請求明細で Business Support+ の請求と返金を確かめる（追加 2026-11-01、期限 2026-12-10、重要 低）
- [ ] 制限の解除後に実績用 ElastiCache `litellm-lab-activity` とサブネットグループを消す（追加 2026-11-01、重要 高）
- [ ] AWS に配備して試す（追加 2026-10-24、重要 高）
- [ ] orca の作業ツリーと `feature/aws-ec2-pair` を消す（追加 2026-11-01、重要 低）
