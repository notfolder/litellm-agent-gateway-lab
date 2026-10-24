# エージェント組み込みの LLM 審査の調査と Qiita 記事の中断
更新: 2026-10-24 10:37 JST
セッション: fa2c338e-6922-4daf-a127-f1d4a11f5da1
ログ: ./session.md
## 概要
ゲートウェイの審査の Qiita 記事を書いたあと、Codex にも Auto-review があると分かって前提が崩れたので記事を削除し、Claude Code・Codex・Copilot の組み込み審査と設定方法を `docs/agent-builtin-review.md` にまとめて main に push した。AWS 構成の検討は依頼の途中で中断。
## やったこと
- 依頼: 「このプロジェクトの監査の仕組み（文脈あり審査と文脈独立審査）を Qiita の記事にするためのインタビューをして記事を作る」。仕様書・設計書・判定基準・試験結果・Anthropic の auto mode の記事・LiteLLM のガードレール文書を読み、資料で決められない 9 点（読者、範囲、動機、すり抜けの実測を書くか、数字、審査官のモデルの書き方、Jev、開発体験、題名）を番号付きでヒアリングした。回答: 読者は社内ゲートウェイの基盤・セキュリティ担当、1 本で中身の審査は概要のみ、動機は「久しぶりに Codex を使ったら auto mode のような守りが無く PC 操作が不安」、すり抜けと数字は書く、審査官は「別の LLM（検証では OpenAI のモデル）」とだけ、Jev と開発体験は書く。
- 記事 `docs/qiita/2026-10-18-litellm-agent-gateway-tool-call-review/article.md`（約 7,200 字、Mermaid 3 枚、表 7 つ）を qiita-writer の手順で書き、リンターのスコア 96。題名は C 案を元に yomiyasu に合わせて「LiteLLMでAIエージェントの行動を守る。許可の無い危険な操作を止め、許可があっても機密情報を持ち出させない仕組みを作った」にした。
- 調査 1「Codex に auto mode のような保護はあるか」: **ある。** Auto-review（内部名 Guardian、OpenAI の公開 2026-04-30、手元の 0.162.1 に `--approve-for-me`）。任意で既定は人の承認。サンドボックスの外へ出る要求だけを審査し、審査官はツールの出力も見る。公開のロードマップは無し。第三者の検証で SSH 鍵の持ち出しが 5 回中 4 回通った報告あり。記事の前提「Codex には守りが無い」が誤りと判明。
- 調査 2「Microsoft 365 Copilot・Copilot Studio にも同様の機能はあるか」: LLM の審査は無く、外部システムに判定を委ねるリアルタイム保護（計画を API で送り、1 秒で応答が無ければ実行する fail-open。Defender や自作システムを接続）と、ツールごとの人の承認（ロードマップ RM570434、GA 予定 2026 年 9 月、状態は開発中のまま）。コンピューター操作の人の監督は現行 V2 で非対応。
- 調査 3「文脈を無視して機密の持ち出しを防ぐ仕組みは」: 決定的なルールとして Claude Code のサンドボックス（ネットワーク許可リスト、`credentials`。既定は切りで、資格情報の既定の禁止一覧は無い）、Codex のネットワーク遮断（既定で入り）、Purview DLP、LiteLLM の Tool Permission ガードレール（`allowed_param_patterns`）がある。「LLM に引数の意味を読ませながら会話を渡さない」中間の作りは製品に無く、研究では CaMeL（情報フロー制御）と OAP（決定的な実行前認可）。OS のサンドボックスの意味（Seatbelt、bubblewrap、seccomp、Landlock）と既定の有効・無効も答えた。
- 文書 `docs/agent-builtin-review.md`（6 章、出典 16 件）を書き、README に案内を足し、BACKLOG に Codex Auto-review あり・なしの比較試験を足した。ブランチ `docs/agent-builtin-review` でコミット `8831c5d` → 指示で main に fast-forward マージ、push、ブランチ削除。
- Qiita の記事は未コミットのまま削除（指示「qiita の記事は一旦削除で」）。
- 依頼「AWS のサービス（CloudFormation、EKS、RDS、S3 など）でこの仕組みを組み立てる構成を検討」: ElastiCache for Valkey の Multi-AZ、RDS Proxy と Prisma の相性（prepared statement と SET でセッションが固定される）、LiteLLM の EKS 配備（一体型チャートが本番向け。マイクロサービス版は実験的）、S3 Object Lock（COMPLIANCE は誰も消せず、CloudFormation では新規バケットだけ）を調べたところで、ユーザーの中断により未回答。
## 決定事項
- Qiita 記事は、Codex に Auto-review がある事実と合わないのでいったん削除。書き直すときは `docs/agent-builtin-review.md` の 1 章と 5 章（組み込みの審査は利用者が外せ、組織が全エージェントに強制できない。会話を見ない審査と宛先の許可リストはゲートウェイだけ）を前提にする。
- 記事の方針（ヒアリングの回答。書き直しに引き継ぐ）: 読者は社内 AI ゲートウェイの基盤・セキュリティ担当。1 本に収め、中身の審査は概要と図 1 枚。すり抜けの実測と auto mode との比較表・待ち時間の表は載せる。審査官のモデルは「別の LLM。検証では OpenAI のモデル」とだけ書き、ChatGPT Plus 経由やモデル名は書かない。Jev の一次判定と、人が決めたこと／AI に任せたことは書く。題名は yomiyasu の規則に合わせる（文末コロンを使わない、和欧文間の空白を入れない）。
- 組み込み審査の文書は PR を作らず main に直接マージして push（指示）。
## 次にやること
- 中断した AWS 構成の検討に答える（作成せず説明だけ）。部品の対応: k3d → EKS、CloudNativePG＋Pooler → RDS for PostgreSQL（Multi-AZ）か Aurora、Pooler の代わりは RDS Proxy だが Prisma の prepared statement と SET で固定されるので PgBouncer を EKS に残す案と比べる、Valkey＋Sentinel → ElastiCache for Valkey（Multi-AZ、replica 必須）、RustFS → S3（Object Lock COMPLIANCE は新規バケットで）、Traefik → ALB（AWS Load Balancer Controller）、Secret → Secrets Manager＋External Secrets、監査アプリ・整形・MCP サーバー → EKS、審査官と Codex 用モデル → Bedrock（Claude Max・ChatGPT Plus は使えない）、Jev は外部送信の可否を確認。CloudFormation 直書きか CDK かは確認事項にする。
- Codex の Auto-review あり・なしで spec 9.2 のシナリオを流し、記事の比較表に Codex の列を足す。
- Qiita 記事を上の決定事項の方針で書き直す。
## バックログ
- [x] (2026-10-18) Claude Code・Codex・Copilot の組み込みの LLM 審査と設定方法を調査し、`docs/agent-builtin-review.md` にまとめて main に push
- [ ] Codex の Auto-review（`--approve-for-me`）あり・なしで spec 9.2 のシナリオを流し、ゲートウェイの審査との重なりと差を測る（追加 2026-10-18、重要 高）
- [ ] Qiita 記事「LiteLLM で AI エージェントの行動を守る」を `docs/agent-builtin-review.md` を前提に書き直す（追加 2026-10-24、重要 低）
- IDEAS: AWS（CloudFormation、EKS、RDS、S3）でこの仕組みを組み立てる構成の検討（追加 2026-10-24。中断、未回答）
