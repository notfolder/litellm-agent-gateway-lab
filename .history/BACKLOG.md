# BACKLOG（litellm-agent-gateway-lab）

## ツールコール審査
概要: .history/2026-10-12-0737-文脈独立審査の実装/summary.md

- [x] (2026-10-12) 文脈独立のツール引数監査（機密情報の持ち出し防止）を実装・試験し、docs に反映して main にマージ（PR #1）
- [ ] 本物のエージェント（Codex、Claude Code）でのシナリオ試験（spec 9.2）。Jev の有無と、文脈独立審査を足したことによる待ち時間の増え方も測る（`./lab review-log`）
  - 2026-10-12: Codex の `codex exec` で A（メールを読む）と D（古いブランチの掃除）を審査あり・なしで比べた。D は審査ありだけが拒否。B・C と Claude Code は、人が `docs/manual-check.md` の手順で確かめる
- [ ] `docs/manual-check.md`（審査を人が確かめる手順）を人が一度通してから、`docs/maintenance.md`・`docs/guardrail-gaps.md` と一緒にコミットする
- [ ] 審査をすり抜けるローカル破壊的操作の対策（`docs/guardrail-gaps.md` 7 章）。機密データチェックの判定基準から「破壊的操作は allow」を外す案など。採否はヒアリングで決める
  - 2026-10-12 検証: `rm -- <file>`（個人情報ファイルの削除）が審査 allow で実際に削除された。`rm -rf`・`git reset --hard`・`git clean -fdx` も審査は allow、Codex のサンドボックスだけが止めた。推論 none の Codex（審査ありキー）で実測
- [ ] 審査なしのキー `user1-codex-noreview`・`user1-claude-noreview`（2026-10-12 発行）を、確認が済んだら消す（手順は `docs/manual-check.md` 6 章）
- [ ] Gmail の MCP のツール一覧がときどき取れない（LiteLLM から workspace-mcp への tools/list が `401 Unauthorized`。同時に複数のエージェントを動かすと多い。2026-10-12）。原因を調べる

## 検証環境
概要: .history/2026-10-12-0737-文脈独立審査の実装/summary.md

- [ ] 作業ツリー `~/orca/workspaces/litellm-agent-gateway-lab/コンテキスト独立tool引数監査` を片付ける（中に `.env` のコピーがある）
- [ ] spec 9.1 の未確認: 7 日後の再サインイン、Claude Code の auto mode で MCP 呼び出しがブロックされないこと
- [ ] spec 9.3 の未確認: OAuth のコールバックが別 Pod でも成功する、chatgpt/ のトークン共有の競合、入口のタイムアウト、無停止のバージョンアップ、Jev 有無の遅延と負荷時の挙動

## 冗長化と監査ログ
概要: .history/2026-10-12-0741-検証環境の構築と冗長化・監査ログ/summary.md

- [x] (2026-10-12) PostgreSQL 3 台（CloudNativePG）・Valkey と Sentinel・RustFS・監査ログ・検索アプリを実装し、試験を通して main に push
- [x] (2026-10-12) Gmail の MCP にサインインし直し、監査ログの試験 27/27（MCP の記録を確認）
- [x] (2026-10-12) 検索アプリのリダイレクト URI を Google Cloud に追加し、ブラウザでのサインインを確認
- [ ] 本番の規模（1 日 1,000 万件）での検索の速さの実測（今は合成データ 1 日 100 万件 × 7 日からの見積もり）
- [ ] 本番に向けて、CloudNativePG のオペレーター・cert-manager・Traefik を複数台にする（検証では server ノードに固定）
- [ ] 無停止のメンテナンスの改善案（`docs/maintenance.md` 4 章）: LiteLLM の PDB と anti-affinity、長いストリーミングとローリング更新の試験、Valkey の更新の手順、`./lab upgrade` での移行の互換性の確認
