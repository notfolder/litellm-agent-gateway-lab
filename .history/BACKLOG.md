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
- [ ] Pod Identity のロールごとの権限（許した操作ができ、許していない操作が拒否される）を、クラスタの中で各 ServiceAccount の Pod から試す試験を `./lab test-audit` に足す（Mac からは IAM ロールを借りられないので今は省いている。変更設計書 10 章）（追加 2026-10-24、重要 中）
- [ ] Codex 用のモデル（Bedrock Mantle の gpt-oss か GPT-5.x、Bedrock の Claude の変換経由、Azure OpenAI）を決める。決まるまで AWS でも `chatgpt/` を使う（トークンは EFS の PVC で共有）（追加 2026-10-24、重要 中）
- [ ] 配備の後、AWS の試験の結果と費用の実測を変更設計書に書き足し、変更仕様書 12 章・変更設計書 12 章の表に従って `docs/spec.md` と `docs/design.md` に反映する（追加 2026-10-24、重要 中）

## VM 3 台構成（`LAB_TARGET=aws-ec2` / `gcp-vm`）
概要: .history/2026-10-31-1220-内部LBとVM側の防御とZabbix/summary.md（ブランチ `feature/aws-ec2-pair`。ワークツリー `~/orca/workspaces/litellm-agent-gateway-lab/feature-aws-target-2`）

- [x] (2026-11-01) 障害の試験の `entry-node`（Traefik の載った台を止める）と `worker-node`（足す台を止める）の目安を決めて反映する — ほかの台を止める場面と同じ 120 秒、GCP 180 秒（ユーザーの判断 1A）。（GCP の実測: entry-node 131.5 秒、worker-node 61.9 秒。どちらも目安 60 秒。案: 台の停止と同じ 120 / 180 秒、primary を先に移す手順、そのまま。`labtool/failover.py` の `LIMITS`、変更仕様書 2026-10-31 R22、変更設計書 2026-10-31-台の増減とLiteLLMのメモリ 7 章）（追加 2026-10-31、重要 高）
- [ ] Mac の k3d を `core.K3S_IMAGE` v1.36.5-k3s1 で作り直して試験が通ることを確かめる（GCP の版上げの計測の後に上げた。k3d では未確認）（追加 2026-10-31、重要 低）

- [ ] k3s の版上げの試験の残り（`.history/2026-10-26-k3s版上げ/変更仕様書.md` 4 章）: パッチの更新、飛ばす・下げる指示、途中で止めて再実行、上げた後の `./lab up`、1 台の作り直し、足す台がある構成（agent の入れ直し。2026-10-31 に実装、未計測）。GCP の v1.35.5 → v1.36.5 の `--measure`（3 台 235 秒、失敗 593/4,680 件、幅 88.3 秒）と Traefik のチャート 40 への対処は 2026-10-31 に済み（変更設計書 2026-10-31 15.1 節）（追加 2026-10-26、重要 高）
- [ ] AMI を新しくする手順（1 台ずつ作り直す）。`resolve_ami` が配備済みの AMI を固定で返すので、放っておくと AMI が古くなる。案: `./lab upgrade-ami` で台ごとに drain → `recreate_node`（managed）→ SSH で初期化（CloudFormation の 3 台を別々の論理 ID にしてあるので、1 台ずつ `Replace` する手立てが要る）（追加 2026-10-26、重要 低）
- [ ] 審査官を Claude Haiku 4.5 にしたときの事例試験の不一致 14 事例（社内宛てを止める、設定ファイルを求めない）を見て、審査官の指示文を詰めるか期待値を見直す（`tests/review-scenarios/`。`./lab test-judge` の記録）（追加 2026-10-30、重要 低）
- [ ] `aws-ec2` の配備と試験（EC2 の vCPU 上限の解除後）。2026-10-31 の変更（provision/setup の分離、1 バケットの Object Lock と IAM の接頭辞のポリシー、キーペアの取り込み、中継の VM とセキュリティグループの egress、S3 ドライバの registry）も AWS では未確認。GCP で直した共通部分（公開 IP の引き直し、known_hosts の識別子、冪等性、監査の試験の入口）は AWS でも効くが、AWS 固有（awscli の snap、netplan の 2 つ目の IP、Elastic IP）は未確認（追加 2026-10-30、重要 高） 2026-10-31 の内部 NLB（固定の内部 IP、ターゲットグループ 2 つ、ヘルスチェック）、ロール無しの EC2、セキュリティグループの単純化、足す台（3 サブネットへの割り振り、ターゲットグループへの登録と解除）も未確認
- [ ] 既存資源（`LAB_INFRA=provided`）を本物の「用意する側が作った物」で試す（足す台の `LAB_VM_WORKER_NODES` での追加と削除も未確認。managed の台数での増減は 2026-10-31 に GCP で確認済み。今回は managed が作った物を provided として読み直しただけ。用意する側への前提 spec 12.2 の文書どおりに人が作って `./lab up` が通るか。GCP の registry の JSON キーの受け渡しも）（追加 2026-10-31、重要 中） 2026-10-31 からは内部ロードバランサと踏み台（`LAB_VM_BASTION_HOST/CIDR`）も用意する側の物で、`LAB_VM_CIDR` に VPC の実際の CIDR を書く前提
- [ ] `./lab down --all`（provided の全削除）の実機確認（今回は managed の `./lab down` で全部消した。保持（Unlocked）を外してバケットを消す経路は未確認）（追加 2026-10-31、重要 中） 2026-10-31 からはロードバランサ（識別子からたどる）も消す経路が加わった
- [ ] Zscaler の実物での確認: 明示プロキシの URL と CA を `.env` に入れて `./lab up` が通るか（検証は mitmproxy。Zscaler 固有の挙動（SNI の検査、特定の宛先の除外、認証付きのプロキシ）は未確認）（追加 2026-10-31、重要 中） 本物の Zscaler では cert-manager の DNS-01 の伝搬確認（クラウドの再帰リゾルバに向けてある）と、Zabbix のリポジトリ・Helm チャートの取得が中継を通るかも確かめる
- [ ] 検証を amd64 の VM でも流す（今回は arm64 だけ。ビルドは VM 上なのでアーキテクチャの違いは `uname -m` の判別だけだが、`google_workspace_mcp` の amd64 のイメージの有無は未確認）（追加 2026-10-31、重要 低）
- [ ] Zabbix に LiteLLM の内部指標を取り込む: LiteLLM の `/metrics` は公式文書が Enterprise の機能と表示しているので無償版で出るか確かめ、出なければ `/health` と監査ログの件数で代える。PostgreSQL は CloudNativePG の状態で監視済み（2026-10-31）（追加 2026-10-31、重要 中）
- [ ] 本物の踏み台での確認: provided の踏み台は操作者が SSH できる前提（`LAB_VM_BASTION_HOST`）。踏み台の SSH が別の経路（Zscaler Private Access など）のときの転送の張り方は未設計（追加 2026-10-31、重要 中）
- [ ] 入口の台を止める場面（`entry-node`）の回復時間と k3s の版上げの切断時間を AWS（内部 NLB、ヘルスチェック 10 秒 × 2）でも測る（GCP の実測は変更設計書 2026-10-31 15.1 節）（追加 2026-10-31、重要 中）

## 冗長化と監査ログ
概要: .history/2026-10-12-0741-検証環境の構築と冗長化・監査ログ/summary.md

- [ ] 本番の規模（1 日 1,000 万件）での検索の速さの実測（今は合成データ 1 日 100 万件 × 7 日からの見積もり）（追加 2026-10-12、重要 低）
- [ ] 本番に向けて、CloudNativePG のオペレーター・cert-manager・Traefik を複数台にする（検証では server ノードに固定）（追加 2026-10-12、重要 低）
  - 2026-10-26: Mac の k3d は Kubernetes の版上げが無停止でできない（k3d に版を上げる命令が無く、server 1 台に制御部分の SQLite と Traefik・RustFS・オペレーターが固定）。無停止にするなら server 3 台（組み込み etcd）とこの固定の解除が要る。ユーザー判断で記録のみ（`docs/maintenance.md` 3.3）。EKS は Auto Mode が PDB を守ってノードを入れ替えるので無停止。LiteLLM に PDB（`maxUnavailable: 1`）を足した
- [ ] 無停止のメンテナンスの改善案（`docs/maintenance.md` 4 章）: LiteLLM の PDB と anti-affinity、長いストリーミングとローリング更新の試験、Valkey の更新の手順、`./lab upgrade` での移行の互換性の確認（追加 2026-10-12、重要 低）
