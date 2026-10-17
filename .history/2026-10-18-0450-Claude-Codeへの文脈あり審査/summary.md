# Claude Code の auto mode とゲートウェイの文脈あり審査の併用（PR #4）
更新: 2026-10-18 08:30 JST
セッション: c802a8a8-7060-40f1-8c4f-bf63feea8b46
ログ: ./session.md

## 概要
Claude Code の auto mode と文脈あり審査を両方掛けたときの影響を検討し、設定 `LAB_CONTEXT_REVIEW_CLAUDE_CODE`（既定 切り）で Claude Code にも掛けられるようにして、判定入力の抽出の欠陥（system-reminder の後ろの指示が落ちる）を直し、実機 165 回で確かめて PR #4 にした。

## やったこと
- 検討（読み取り専用）: Claude Code の公開文書と 2026-10-17 の実機の監査ログから、ゲートウェイ経由の auto mode は通常の要求の `safeguards` 項目で判定を依頼する「サーバー側審査」で、分類器の別要求は来ないこと、LiteLLM がこの項目を素通しし文脈独立審査と既に同居していることを確かめた。実機の要求では最初の user メッセージが system-reminder 3 ブロック（約 8,300 字）＋利用者の指示 132 字で、従来の抽出（結合して先頭 2,000 字）では指示が落ちることを見つけた。
- 実装（ブランチ `feature/context-review-claude-code`、コミット `230ab5c`）: ガードレールの設定 `context_review_claude_code`（`.env` の `LAB_CONTEXT_REVIEW_CLAUDE_CODE`、既定 false）と `_plan` の分岐、ユーザー役の text ブロックごとに 1 件にする抽出、疎通と監査ログの試験の期待、単体テスト 7 本。
- 試験: 単体 40＋95、疎通 75（設定の切り・入り）、監査ログ 33、実機（`claude -p`、11 シナリオ × 3 設定 × 5 回 = 165 回、約 65 分）。結果は `docs/design.md` 16.7。
- 文書: spec（決定事項、4.2、4.3、4.7、9.2、10 章）、design（6.3、9.1、9.2、9.3、9.10、16.1、16.7）、README、manual-check、`変更要件.md`（この場所）。`~/.claude/estimate-log.md` に 1 行（見積もり 2.6 時間、実績 約 1.9 時間、比 0.73。ホームのセッションでコミットする）。
- push と PR #4 の作成（https://github.com/notfolder/litellm-agent-gateway-lab/pull/4 ）。
- 意図的に残したもの: 実機試行の実行・集計スクリプト（`runner.py`、`analyze.py`、`scan_fallback.py`）はセッションの作業領域に置き、リポジトリに入れていない。検証環境の `.env` は `LAB_CONTEXT_REVIEW_CLAUDE_CODE=true` のまま。

## 決定事項
- Claude Code への文脈あり審査は設定で切り替え、**既定は切り**（試験の間だけ入れる。本番の守りは組織が選ぶ）。
- 許可の言葉の基準は**今のまま**（失われるもの・外部に出るものを認める言葉が要る。auto mode の「名指しの指示で通る」には合わせない）。push と PR が許可の言葉なしで止まるのは承知のうえ。
- 抽出の修正は **text ブロックごとに 1 件**（クライアント固有の処理を足さない）。
- 「検討して」の依頼ではブランチも作らず報告だけにし、方針が決まってからブランチを切った。

## 次にやること
- PR #4 のマージと main への復帰（このセッションで実施）。
- 検証環境の `.env` の `LAB_CONTEXT_REVIEW_CLAUDE_CODE=true` を既定に戻すか決める（戻すなら `false` にして `./lab litellm`）。
- Codex で同じ 11 シナリオと Jev 有無の比較（spec 9.2 の残り）。

## バックログ
- [x] (2026-10-18) Claude Code の auto mode と文脈あり審査の併用の検討、設定の追加、抽出の修正、実機 165 回、PR #4
- [ ] Claude Code に文脈あり審査を掛けたときの push・PR 作成の誤ブロック（許可の言葉が無いと止まる）。実運用で不便なら、審査官に許可リストの宛先と作業中のリポジトリの remote を「許可なしで送ってよい宛先」として渡す案を検討する
- [ ] 検証環境の `.env` の `LAB_CONTEXT_REVIEW_CLAUDE_CODE=true` を、試験が済んだら既定（false）に戻すか決める
- [ ] 本物のエージェントでのシナリオ試験の残り: Codex での同じシナリオと Jev 有無の比較
