# BACKLOG（litellm-agent-gateway-lab）

完了と見送りは `BACKLOG-archive.md`、完了条件の無い案は `IDEAS.md` にある。

## ツールコール審査
概要: .history/2026-10-24-1037-組み込み審査の調査とQiita記事の中断/summary.md

- [ ] 本物のエージェント（Codex、Claude Code）でのシナリオ試験（spec 9.2）。Jev の有無と、文脈独立審査を足したことによる待ち時間の増え方も測る（`./lab review-log`）（追加 2026-10-12、重要 高）
  - 2026-10-12: Codex の `codex exec` で A（メールを読む）と D（古いブランチの掃除）を審査あり・なしで比べた。D は審査ありだけが拒否。2026-10-18: Claude Code は 11 シナリオ × 3 設定 × 5 回を流し design 16.7 に書いた。残りは Codex での同じシナリオと Jev 有無の比較
- [ ] Codex の Auto-review（`--approve-for-me`）あり・なしで spec 9.2 のシナリオを流し、ゲートウェイの審査との重なりと差を測る（design 16.7 の Claude Code の試験と同じ形）（追加 2026-10-18、重要 高）
- [ ] 検証環境の `.env` の `LAB_CONTEXT_REVIEW_CLAUDE_CODE=true` を、試験が済んだら既定（false）に戻すか決める（追加 2026-10-18、重要 高）
- [ ] `docs/manual-check.md`（審査を人が確かめる手順）を人が一度通す（2026-10-13 にユーザーの指示でいったん中止。審査なしキーは `.env` の `LAB_NOREVIEW_KEYS=true` で用意）（追加 2026-10-13、重要 低）
  - 2026-10-13: 1 回目は無効。審査なしの Codex と同じ `CODEX_HOME` の常駐プロセスが審査なしのキーを持ち続けていた。審査なしの `CODEX_HOME` を `.codex-lab-noreview` に分けた
- [ ] 審査なしの Codex の `CODEX_HOME` 分離（`.codex-lab-noreview`、コミット `67d37f3`）を、人の確認が済んだら `docs/design.md`（15 章のクライアント設定、ディレクトリ構成）に反映する（追加 2026-10-16、重要 低）
- [ ] Codex（ChatGPT デスクトップ）の Computer Use プラグインと Claude Code の computer-use を実機でゲートウェイに通し、届くツール名と引数の形を監査ログで確かめて事例に足す（公開文書にツール名が無い。Claude Cowork は接続先を変えられないので対象外）（追加 2026-10-18、重要 低）
- [ ] 本物の Claude Code で、方針の追記に従って Bash の `description` を省くかを確かめる（省かないと一度止まり、案内で直る）。合わせて、複数ファイルの送信で読み取りの出力が切れたときに「一度に送るファイルを減らすか、操作を分ける」の案内で止まり、読み直しを繰り返さないことを確かめる（追加 2026-10-18、重要 低）
- [ ] 実運用に向けて `LAB_ISOLATED_DESTINATIONS` に GitHub とメールの宛先を足す手順を `docs/manual-check.md` 以外（運用の文書）にも書く（追加 2026-10-17、重要 低）
- [ ] 画像を送る操作を、審査官（LLM）が画像を見て判定するように広げる（今は画像は止める。Codex の画像を読むツールを実物で確かめる必要がある）（追加 2026-10-18、重要 低）
- [ ] Gmail の MCP のツール一覧がときどき取れない（LiteLLM から workspace-mcp への tools/list が `401 Unauthorized`。同時に複数のエージェントを動かすと多い）原因を調べる（追加 2026-10-12、重要 低）

## 記事と文書
概要: .history/2026-10-24-1037-組み込み審査の調査とQiita記事の中断/summary.md

- [ ] Qiita 記事「LiteLLM で AI エージェントの行動を守る」を、`docs/agent-builtin-review.md` の 1 章と 5 章を前提に書き直す（ヒアリングの回答は summary.md の決定事項。Codex の Auto-review あり・なしの比較試験の後）（追加 2026-10-24、重要 低）

## 検証環境
概要: .history/2026-10-12-0737-文脈独立審査の実装/summary.md

- [ ] 作業ツリー `~/orca/workspaces/litellm-agent-gateway-lab/コンテキスト独立tool引数監査` を片付ける（中に `.env` のコピーがある）（追加 2026-10-12、重要 高）
- [ ] spec 9.1 の未確認: 7 日後の再サインイン、Claude Code の auto mode で MCP 呼び出しがブロックされないこと（追加 2026-10-12、重要 低）
- [ ] spec 9.3 の未確認: OAuth のコールバックが別 Pod でも成功する、chatgpt/ のトークン共有の競合、入口のタイムアウト、無停止のバージョンアップ、Jev 有無の遅延と負荷時の挙動（追加 2026-10-12、重要 低）

## AWS 構成
概要: .history/2026-10-24-AWS構成/変更仕様書.md（ブランチ `feature/aws-target`）

- [ ] 変更仕様書の「13. 前提」（リージョン東京、検証の AWS 環境は GOVERNANCE と Google SSO）をユーザーに確認し、確定したら 0.2 の決定事項に移す（追加 2026-10-24、重要 高）
  - 2026-10-24: 回答待ちのまま実装に入った。コードは東京・GOVERNANCE・Google SSO の前提（`.env` で変えられる）
- [ ] AWS に配備して試す（資格情報の用意が先。手順は変更設計書 7 章）: `LAB_TARGET=aws` で `./lab up` → `test-smoke` → `test-idempotency` → `test-audit` → `test-failover`（litellm-drain、aurora-failover、valkey-failover）→ `test-judge`（Haiku 4.5 の審査官の評価セットの差を記録）→ `./lab down` で計算資源を消して作り直し、S3 と Aurora のデータが残ることを確かめる（追加 2026-10-24、重要 高）
  - 2026-10-24: IAM Identity Center を東京に有効化（単一リージョン、組織インスタンス。Organizations も同時に作成）、利用者 `notfolder` と許可セット `AdministratorAccess`（セッション 12 時間）を作ってアカウントに割り当てた。残りは招待メールの承諾・MFA、`aws configure sso`、Bedrock のモデルアクセス
  - 2026-10-24: `aws configure sso`（プロファイル `litellm-lab`）まで完了。Bedrock はアカウント全体が `Error 002: Access to Bedrock models is not allowed for this account`（Nova でも、東京・バージニアでも同じ。SCP は既定のみ）で、Anthropic の利用目的フォームも「not authorized」。サポートケース 179284818700912（アカウントと請求 / Account / Other Account Issues、英語、ウェブ）で解除を依頼中。解除後に利用目的フォーム（個人開発者、GitHub の URL、Software as a Service、Internal users）を送り直す
  - 2026-10-24 23:48: 配備は CloudFormation の 4 スタックまで通った（cert は ACM 発行済み、Cloudflare に検証用 CNAME 追加済み、network、data）。EKS のノードが EC2 の vCPU 上限 0（オンデマンド Standard・スポットとも。既定は 5）で起動できず `FleetQuotaExceeded`。Service Quotas に 16 vCPU の引き上げを申請（ID 4a733396241d475f8b46f02a9a2973f8cLD9swzf、PENDING）。待つ間は `./lab down` で compute だけ削除済み（cert・network・data は残る）。支払いカードの期限切れを更新済み（反映に最長 1 日）。承認されたら `./lab up` で EKS から再開 → ALB の DNS 名を Cloudflare に CNAME（DNS のみ）で追加
  - 2026-10-25 00:19: ユーザー指示「`./lab down` で消えない有料リソースがあるのは困る」により、`./lab down` を「compute・data・network のスタックと残留物（S3、秘密情報、スナップショット、ECR、EFS、ロググループ）をすべて消す」に変更（コミット b60c0f9）。実機で流して、AWS に残るのは cert の ACM 証明書（無料）、CDK の bootstrap（CDKToolkit。数 KB の S3 と空の ECR）、Identity Center、Cloudflare の CNAME だけになったことを確認。再開時は `./lab up` が 4 スタックの作成からやり直す（bootstrap と証明書は済んでいる）
  - 2026-10-27 21:39: 待ち 3 日目。EC2 の vCPU 上限は 0、引き上げ申請は `CASE_OPENED`（更新は 10/24 23:47 が最後）、Bedrock は `Error 002` のまま。サポートケース 2 件（179284818700912、179285325800171）はどちらも「アサイン待ち」で AWS からの返答なし。サポートセンターに日本語窓口の混雑の案内あり。Basic プランで回答期限の約束は無い
  - 2026-10-29 13:40: 待ち 5 日目、変化なし。re:Post の 2026 年の事例を調べた結果、新規アカウントの Bedrock ブロック・上限 0 は「担当者が付くまで 1〜2 週間、解決はさらに先」が中心で、「利用実績を 1 請求サイクル積んでから」で却下される例もある。Developer サポートは 2025-12-02 に新規受付終了（後継 Business Support+ 月 29 ドル）で、有料でも解除が早まった実績は無し（見送り）。ユーザー承認で 2 件のケースに英語で追記（進捗確認と Manager Escalation の依頼、EC2 は 16 が無理なら既定の 5 でも可と記載）。次の手は 1〜2 週間返答が無ければ re:Post 投稿（投稿はユーザー、下書きは応答に提示済み）
  - 2026-10-30 00:06: 「利用実績を作る」ための計算資源を探したが、EC2・Fargate・Lightsail（VM）・App Runner はすべて作成不可（Lightsail は `maximum limit of Lightsail Instances : 0`、App Runner は `Account ... can't create new App Runner services`）。SageMaker ノートブック・CodeBuild・EMR Serverless も上限 0。残るのは Lambda（同時実行 10）と管理サービス（RDS 40、ElastiCache 300、EKS 制御部分）。試行で課金は無し。App Runner の自動スケーリング設定 1 件が inactive で残る（無料）。予算は月 30 ドル以内（ユーザー指示）
  - 2026-10-30 00:11: 利用実績のため、ユーザー指示で ElastiCache Valkey 9.0 の cache.t4g.micro × 1（レプリケーショングループ `litellm-lab-activity`、東京 1c、約 0.02 ドル/時 ≒ 月 15 ドル）を置いた。既定 VPC が無かったので `create-default-vpc` で作り直し（vpc-028221a714e04d768、無料）、サブネットグループ `litellm-lab-activity`（1c・1d。1a は unavailable、1b は不可）。**上限が解除されたら消す**: `aws elasticache delete-replication-group --replication-group-id litellm-lab-activity` → `delete-cache-subnet-group`。既定 VPC は `./lab up` の network スタックと無関係なので残しても可。ユーザー承認でケース 179284818700912 に「Lightsail・App Runner・Fargate も不可、ElastiCache は可。アカウント単位の保留に見えるので Trust & Safety の審査が要るなら必要な情報を教えてほしい」を英語で追記（10/30 00:30）
  - 2026-10-31 再確認（7 日目）: EC2 On-Demand/Spot と Fargate の vCPU 上限 0、上限申請 CASE_OPENED（更新 10/24 23:47 のまま）、Bedrock Error 002、ともに変化なし。ケース 2 件とも「アサイン待ち」で AWS の返答なし（メール通知 3 件は自分の追記分）。ElastiCache の実績用ノードは available。Support Center に新表示: 日本語窓口の「アカウントと請求」が混雑中で回答に通常より時間がかかる旨の案内、「2026-11-16 までに AWS サポートの許可（IAM 権限）を更新」の通知、ビジネスサポートプラスの期間限定トライアル（返金あり）の広告
  - 2026-10-31 ビジネスサポートプラスの「期間限定トライアル」の条件を調査（未申込）: 申込画面 `console.aws.amazon.com/support/plans/home#/credit-request?modifierId=promotion6fc5ce`。標準価格（月 29 ドル以上、利用額の 10% と大きい方）が先に請求され、2 か月分（60 日）を自動で返金（カードならマイナス計上。規約では請求書の支払い完了後、最長 90 日）。30 日の契約、30 日ごと自動更新、60 日を過ぎると通常課金。対象は「current and in good standing」のアカウントで、AWS が申込後に適格性を確認。オファー期間 2025-12-01〜2026-12-31。旧来の無料期間（10/31 まで無料＋174 ドルのクレジット）は新方式のアカウント（settings.aws.com）向けで、このアカウントには当てはまらない
  - 2026-10-31 ユーザー承認（1A・2B）で私がトライアルを申し込み、プランが「AWS ビジネスサポートプラス」になった（コンソール表示「トライアルは 12/31/2026 に終了」「毎月最初の 5〜7 営業日以内に返金」）。DevOps エージェント有効化のダイアログは IAM ロールを作るので閉じた（未有効化）。Support API が使えるようになり `aws support describe-cases --region us-east-1 --include-communications` でケースを読める（Chrome 不要）。**解約期限: 2026-12-28 まで（加入から 60 日の 12/29 より前）に `aws support` またはサポートプランのページで Basic に戻す**。12 月初旬に 11 月分の請求で 29 ドルの請求と返金（マイナス計上）が出ているか確認する。10/31 加入なので 10 月分の日割りが「2 か月」の 1 か月目に数えられる恐れあり → 12 月の明細で確かめる
  - 2026-10-31 08:42 ユーザー承認（1A・2A）で Support API から投稿: ケース 179284818700912 と 179285325800171 に「Business Support+ に上げたので再確認とエスカレーションを」を追記。技術ケース **179340373000600**（Bedrock / models、重大度 Normal、英語、work-in-progress）を新規起票し、Error 002 の再現手順・計算資源も 0 である事実・既存 2 ケースの番号・用途を記載。API で見ると EC2 の上限ケースの分類は `account-verification`（アカウント確認待ちの扱い）。以後の再確認は `aws support describe-cases --region us-east-1 --include-communications` で読む
  - 2026-10-31 08:48 技術ケース 179340373000600 に AWS の自動応答（生成 AI）: 「アカウント単位の制限であり IAM・SCP の問題ではない。計算資源の上限 0 も同じ制限。コンソールでモデル契約を試し、ダメなら既存 2 ケースで対処」として pending-customer-action に変更（放置すると自動解決）。確認: `get-foundation-model-availability` は Claude Haiku 4.5 が agreement NOT_AVAILABLE / authorization NOT_AUTHORIZED、Nova Micro は agreement AVAILABLE でも NOT_AUTHORIZED、`PutUseCaseForModelAccess` も「not authorized」。ユーザー指示で返信（08:52）: 自己解決は不可能、自動解決しないこと、人のエンジニアへの割り当てとアカウント制限を扱うチーム（Trust & Safety / account verification）へのエスカレーション、確認に必要な情報の明示、どのケースで結論を出すかの明示を要求。状態は customer-action-completed
  - 2026-10-31 08:58 訂正: 返信に「account created in October 2026」と未確認のまま書いたが誤り。AWSServiceRoleForSupport の作成日が 2018-08-24（Trusted Advisor は 2018-11-20）で、**アカウントは 2018 年作成の休眠アカウント**（13 か月の利用料 0、CloudTrail は 10/24 以前なし、カード期限切れ）を 10/24 に再開したもの（ユーザー確認済み 1A）。ユーザー承認（2A）で技術ケース 179340373000600 に訂正を投稿し、休眠期間・期限切れカードが原因なら現在の支払い方法で再確認してほしいと依頼。今後ケースに書く事実は投稿前に API で裏を取る
- [ ] Pod Identity のロールごとの権限（許した操作ができ、許していない操作が拒否される）を、クラスタの中で各 ServiceAccount の Pod から試す試験を `./lab test-audit` に足す（Mac からは IAM ロールを借りられないので今は省いている。変更設計書 10 章）（追加 2026-10-24、重要 中）
- [ ] Codex 用のモデル（Bedrock Mantle の gpt-oss か GPT-5.x、Bedrock の Claude の変換経由、Azure OpenAI）を決める。決まるまで AWS でも `chatgpt/` を使う（トークンは EFS の PVC で共有）（追加 2026-10-24、重要 中）
- [ ] 配備の後、AWS の試験の結果と費用の実測を変更設計書に書き足し、変更仕様書 12 章・変更設計書 12 章の表に従って `docs/spec.md` と `docs/design.md` に反映する（追加 2026-10-24、重要 中）
- [x] (2026-10-24) ElastiCache の `durability: sync` は `cache.t4g.micro` で使えなかった（Graviton `.large` 以上とクラスターモード有効が必須）。`min-replicas-to-write 1` に変更（ユーザー回答 1A）
- [x] (2026-10-24) 変更設計書 `.history/2026-10-24-AWS構成/変更設計書.md` を書く
- [x] (2026-10-24) 実装: `LAB_TARGET` の切り替え、`labtool/aws.py`、`cdk/`、S3 の資格情報チェーンとバケット名の接頭辞、ESO、ALB の Ingress、Bedrock のモデル登録、障害試験の AWS の場面。単体テスト（labtool 52、監査 19、監査ログ 12）と CDK の synth が通り、Mac の `./lab up` を流し直して確認

## 冗長化と監査ログ
概要: .history/2026-10-12-0741-検証環境の構築と冗長化・監査ログ/summary.md

- [ ] 本番の規模（1 日 1,000 万件）での検索の速さの実測（今は合成データ 1 日 100 万件 × 7 日からの見積もり）（追加 2026-10-12、重要 低）
- [ ] 本番に向けて、CloudNativePG のオペレーター・cert-manager・Traefik を複数台にする（検証では server ノードに固定）（追加 2026-10-12、重要 低）
  - 2026-10-26: Mac の k3d は Kubernetes の版上げが無停止でできない（k3d に版を上げる命令が無く、server 1 台に制御部分の SQLite と Traefik・RustFS・オペレーターが固定）。無停止にするなら server 3 台（組み込み etcd）とこの固定の解除が要る。ユーザー判断で記録のみ（`docs/maintenance.md` 3.3）。EKS は Auto Mode が PDB を守ってノードを入れ替えるので無停止。LiteLLM に PDB（`maxUnavailable: 1`）を足した
- [ ] 無停止のメンテナンスの改善案（`docs/maintenance.md` 4 章）: LiteLLM の PDB と anti-affinity、長いストリーミングとローリング更新の試験、Valkey の更新の手順、`./lab upgrade` での移行の互換性の確認（追加 2026-10-12、重要 低）
