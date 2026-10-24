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

## 冗長化と監査ログ
概要: .history/2026-10-12-0741-検証環境の構築と冗長化・監査ログ/summary.md

- [ ] 本番の規模（1 日 1,000 万件）での検索の速さの実測（今は合成データ 1 日 100 万件 × 7 日からの見積もり）（追加 2026-10-12、重要 低）
- [ ] 本番に向けて、CloudNativePG のオペレーター・cert-manager・Traefik を複数台にする（検証では server ノードに固定）（追加 2026-10-12、重要 低）
- [ ] 無停止のメンテナンスの改善案（`docs/maintenance.md` 4 章）: LiteLLM の PDB と anti-affinity、長いストリーミングとローリング更新の試験、Valkey の更新の手順、`./lab upgrade` での移行の互換性の確認（追加 2026-10-12、重要 低）
