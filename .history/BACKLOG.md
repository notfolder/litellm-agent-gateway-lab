# BACKLOG（litellm-agent-gateway-lab）

## ツールコール審査
概要: .history/2026-10-12-0737-文脈独立審査の実装/summary.md

- [x] (2026-10-12) 文脈独立のツール引数監査（機密情報の持ち出し防止）を実装・試験し、docs に反映して main にマージ（PR #1）
- [ ] 本物のエージェント（Codex、Claude Code）でのシナリオ試験（spec 9.2）。Jev の有無と、文脈独立審査を足したことによる待ち時間の増え方も測る（`./lab review-log`）

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
