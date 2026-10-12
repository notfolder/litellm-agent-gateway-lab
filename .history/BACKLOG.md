# BACKLOG（litellm-agent-gateway-lab）

## ツールコール審査
概要: .history/2026-10-13-0000-監査の許可ベース化と審査なしキー自動化/summary.md

- [x] (2026-10-12) 文脈独立のツール引数監査（機密情報の持ち出し防止）を実装・試験し、docs に反映して main にマージ（PR #1）
- [x] (2026-10-13) 審査をすり抜けるローカル破壊的操作の対策。監査の判定を許可ベースに変更（取り返しのつかない操作は許可の明示がある場合だけ allow）、Jev の問いも直して main に push（`2ac468f`）。検証は `docs/guardrail-gaps.md`
- [x] (2026-10-13) 審査なしのキーを `./lab`（`LAB_NOREVIEW_KEYS`）で作る／消すようにして push（`a5600d3`）。手で発行した旧キーは削除済み
- [ ] 本物のエージェント（Codex、Claude Code）でのシナリオ試験（spec 9.2）。Jev の有無と、文脈独立審査を足したことによる待ち時間の増え方も測る（`./lab review-log`）
  - 2026-10-12: Codex の `codex exec` で A（メールを読む）と D（古いブランチの掃除）を審査あり・なしで比べた。D は審査ありだけが拒否。B・C と Claude Code は、人が `docs/manual-check.md` の手順で確かめる
- [ ] `docs/manual-check.md`（審査を人が確かめる手順）を人が一度通す（2026-10-13 にユーザーの指示でいったん中止）（審査なしキーは `.env` の `LAB_NOREVIEW_KEYS=true` で用意）
  - 2026-10-13: 1 回目は無効。審査なしの Codex と同じ `CODEX_HOME` の常駐プロセス（app-server）が審査なしのキーを持ち続け、審査ありの Codex もそのキーで送っていた。審査なしの `CODEX_HOME` を `.codex-lab-noreview` に分け、2.3 の試験用リポジトリにシナリオの材料を足した
- [ ] 審査なしの Codex の `CODEX_HOME` 分離（`.codex-lab-noreview`）を、人の確認が済んだら `docs/design.md`（15 章のクライアント設定、ディレクトリ構成）に反映する
- [ ] 機密データチェックで、名前から機密か分からないファイルを外へ送る操作の中身を確かめる（ヒアリング中。未実装）
  - 仕様と設計: `.history/2026-10-13-送信するファイルの中身の審査/変更仕様書.md`、`変更設計書.md`。決定事項は仕様 2 章、未決は仕様 13 章
- [ ] 画像を送る操作を、審査官（LLM）が画像を見て判定するように広げる（上の項目では画像は止める。Codex の画像を読むツールを実物で確かめる必要がある）
- [ ] Kiro など、接続先を変えられずゲートウェイを通らないエージェントに審査を掛ける方法（エージェント側のフックからゲートウェイの審査を呼ぶなど）を検討する
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
