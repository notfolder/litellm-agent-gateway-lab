# エージェント組み込みの LLM 審査の調査（Claude Code・Codex・Copilot）

調査日: 2026-10-18

Claude Code、Codex、Microsoft 365 Copilot と Copilot Studio が、エージェント自身の中に持つ「ツールコールを実行前に審査する仕組み」と、その設定方法をまとめた調査です。ゲートウェイの審査（spec 4 章）が何を上乗せしているかを位置づけるために書きました。出典は末尾にあり、本文の記述はすべて出典の公開文書で確かめたものです。第三者の記事に基づく部分はその旨を書いています。

## 1. 要約

| | Claude Code | Codex | Microsoft 365 Copilot / Copilot Studio |
|---|---|---|---|
| LLM による実行前の審査 | **auto mode**（分類器） | **Auto-review**（内部名 Guardian） | 無し。外部システムに判定を委ねる**リアルタイム保護**と、**人の承認**の設定 |
| 既定で有効か | v2.1.283 以降は対話セッションの既定。`claude -p` は既定で Manual | **任意**。既定は人が承認（`approvals_reviewer = "user"`） | リアルタイム保護は外部システムを接続したときだけ。人の承認はツールごとの設定 |
| 審査する対象 | 読み取り系とプロジェクト内編集を除く全ツールコール | **サンドボックスの外へ出る要求だけ**（権限の昇格、遮断されたネットワーク、作業場所外の書き込み、承認が要る MCP） | ツール呼び出し全般（接続した外部システムが判定） |
| 審査官が見るもの | ユーザー発言とツールコール本体のみ（アシスタントの文章とツールの結果は除く。「推論ブラインド」） | 圧縮した会話。ユーザー発言、表示されたアシスタントの文章、関連するツールコールと**その出力**、提案された操作。隠れた推論は含まない | ユーザーのプロンプトと会話履歴、ツール名と入力値、エージェント ID などのメタデータ |
| 審査官のモデル | Sonnet 4.6 | GPT-5.4 Thinking（low） | 接続先に依存（Defender は脅威インテリジェンスと行動分析） |
| 判定基準の差し替え | `autoMode` 設定（`environment`、`allow`、`soft_deny`、`hard_deny`。散文で書く） | `[auto_review].policy`（個人）、`guardian_policy_config`（組織）。全文置き換え | 外部システム側 |
| 拒否のあと | ツールの結果として理由を返す。連続 3 回／累計 20 回で人に戻す | 理由を返す。連続 3 回、または直近 50 回中 10 回の拒否で手番を中断 | エージェントを止めて利用者に知らせる |
| 公表の数字 | 誤ブロック率 0.4%、見逃し率 17%（実トラフィック） | 昇格した操作の自動承認率 99.1%、出しゃばりの検出 90.3%、注入の検出 99.3%（全カテゴリ込み 90.2%） | 公表なし |
| 組織が強制できるか | managed settings で `permissions.defaultMode`、`disableBypassPermissionsMode`、`autoMode` を配布 | 管理設定で `allowed_approvals_reviewers`、`allowed_sandbox_modes`、`guardian_policy_config` | Copilot Studio の管理設定と Defender のポリシー |

3 製品とも、審査はエージェントの中にあり、利用者の設定で外せる（Claude Code は `bypassPermissions`、Codex は `approval_policy = "never"` や `--yolo`）。組織が縛るには各製品の管理設定が要る。ゲートウェイの審査（spec 4 章）は、通る全エージェントに同じ基準を掛け、会話を見ない文脈独立審査と宛先の許可リスト、送るファイルの中身の審査を足している。ただしゲートウェイが見られるのはゲートウェイを通るツールコールだけで、Copilot Studio のエージェントの中で起きるツール呼び出しには届かない（3 章）。

## 2. Claude Code の auto mode

### 2.1 仕組み

Anthropic の解説記事（出典 1）による。

- **入力層**: ファイルの読み取り、Web の取得、シェルの出力がエージェントの文脈に入る前に、サーバー側で注入の兆候を探し、疑わしければ「利用者が実際に頼んだことに立ち返る」よう警告を足す
- **出力層**: 実行前の分類器（Sonnet 4.6）が各操作を判定する。プロジェクト内の編集と安全な読み取り系ツールは分類器を通さない
- **分類器が見るもの**: ユーザー発言とツールコールだけ。アシスタントの文章とツールの出力は除く（「reasoning-blind by design」）
- **二段階判定**: 一次はブロック寄りの単一トークンの可否判定、二次は一次で引っかかったものだけを思考の連鎖で再判定
- **拒否**: ツールの結果として理由を返し、回り道ではなく安全な手を探させる。連続 3 回か累計 20 回で止めて人に戻す。`claude -p` では終了する
- **公表値**: 実トラフィック 1 万件で誤ブロック率 0.4%、実際の出しゃばり操作 52 件の見逃し率 17%、合成の持ち出し 1,000 件の見逃し率 5.7%

分類器は既定で、作業ディレクトリと現在のリポジトリの remote だけを信頼する。会社の source control への push やチームのバケットへの書き込みは、`autoMode.environment` に足すまで止まる。リポジトリ内の任意のブランチへの push と PR の作成は既定で許可される（`production` や `release` のような配置先を示す名前のブランチは別に判定）。

### 2.2 設定方法

| 目的 | 方法 |
|---|---|
| 1 セッションだけ auto mode で始める | `claude --permission-mode auto`（`-p` でも同じフラグ） |
| セッション中に切り替える | `Shift+Tab` で巡回 |
| 既定にする | `~/.claude/settings.json` か managed settings の `permissions.defaultMode: "auto"`。プロジェクトの `.claude/settings.json` に書いた `auto` と `bypassPermissions` は効かない |
| 組織で auto mode を選べなくする | managed settings で `permissions.disableAutoMode: "disable"` |
| 組織で `bypassPermissions` を禁止する | managed settings で `permissions.disableBypassPermissionsMode: "disable"` |
| 信頼する基盤を教える | `~/.claude/settings.json` か managed settings の `autoMode.environment`。`"$defaults"` を入れて既定を残し、散文で足す（例: `"Trusted internal domains: *.corp.example.com"`）。`/auto-mode-setup` で下書きを作れる |
| 判定の規則を足す | `autoMode.allow`（soft block の例外）、`autoMode.soft_deny`（利用者の意図で解ける破壊的操作）、`autoMode.hard_deny`（無条件の境界）。`"$defaults"` を省くとその節の既定を全部捨てる |
| 分類器の前で確実に止める・確認する | `permissions.deny` / `permissions.ask`（例: `"Bash(git push *)"`）。分類器より先に評価され、分類器も利用者の意図も上書きできない |
| 許可ルールに関わらず全シェル命令を分類器に通す | `autoMode.classifyAllShell: true` |
| 有効な規則を見る | `claude auto-mode config`（既定は `claude auto-mode defaults`） |

設定例:

```json
{
  "permissions": {
    "defaultMode": "auto",
    "ask": ["Bash(git push *)", "Bash(gh pr create *)"]
  },
  "autoMode": {
    "environment": [
      "$defaults",
      "Source control: github.example.com/acme-corp and all repos under it",
      "Trusted internal domains: *.corp.example.com"
    ],
    "hard_deny": [
      "$defaults",
      "Never send repository contents to third-party code-review APIs"
    ]
  }
}
```

分類器は `autoMode` をプロジェクトの `.claude/settings.json` と `.claude/settings.local.json` からは読まない（リポジトリに入った設定で許可を注入されないため）。ゲートウェイ経由の Claude Code では、分類器の判定は通常の要求の `safeguards` 項目で依頼され応答の中で返るので、ゲートウェイに分類器の別要求は来ない（design 16.7）。

### 2.3 補足: OS のサンドボックス

分類器とは別に、シェル命令を OS の機能（macOS は Seatbelt、Linux は bubblewrap と seccomp）で囲うサンドボックスがある。**既定は切り**で、`/sandbox` か `sandbox.enabled: true` で入れる。入れると、書き込みは作業ディレクトリと一時ディレクトリに限られ、ネットワークはプロキシ経由で `network.allowedDomains`（空から始まる）だけに通る。資格情報ファイルと環境変数は `sandbox.credentials` に列挙した分だけ守られ、**既定の禁止一覧は無い**。対象はシェル命令だけで、Read・WebFetch・MCP サーバーは外。失敗した命令をサンドボックス外で再試行する逃げ道が既定で有効（`allowUnsandboxedCommands`）で、`false` にすると閉じられる。

## 3. Codex の Auto-review

### 3.1 仕組み

OpenAI の Alignment ブログ（出典 4、2026-04-30）と公式ドキュメント（出典 5、6）による。

- サンドボックスの境界での**人の承認を、別のエージェントによる審査に置き換える**。「Auto-review is a reviewer swap, not a permission grant」で、書き込み可能な場所やネットワークの許可を広げない
- 審査する場面は、昇格した権限を求めるシェル実行、サンドボックスやポリシーが遮断したネットワーク要求、作業場所外のファイル編集、承認が要る MCP やアプリのツール呼び出し。**サンドボックスの中で許されている操作は審査しない**。Computer Use のアプリの承認は人に直接行く
- 審査官は GPT-5.4 Thinking（low）。圧縮した会話（ユーザー発言、表示されたアシスタントの文章、関連するツールコールと出力、提案された操作）を見る。隠れた推論は含まない。読み取り専用の確認を自分で行うこともある（まれ）
- 止めるもの: 私的なデータ・秘密・資格情報を信頼できない宛先へ送る、資格情報・トークン・Cookie・セッションの探索、広範または永続的なセキュリティの低下、取り返しのつかない損害の危険が大きい破壊的操作
- 拒否は理由付きで本体に返り、同じ結果を回り道で求めないよう指示される。連続 3 回、または同じ手番の直近 50 回中 10 回の拒否で手番を中断
- 公表値: 手動の承認モードより承認で止まる回数が約 200 分の 1。昇格した操作の自動承認率 99.1%、全操作で 99.93%。出しゃばりの検出 90.3%、注入の検出 99.3%（破壊的な削除やデータの改ざんを含む全カテゴリで 90.2%）。red-teaming で審査官が誤って承認する例が見つかっており、「セキュリティの保証として扱うべきでない」と明記
- 2026 年 7 月に判定プロンプトの更新（v0.144.0）で private リポジトリへの `git push` を持ち出しと誤判定し、v0.144.2 で取り消した（第三者の記事、出典 8）
- 第三者の検証（出典 7、2026-08-20、Codex CLI 0.148.0）: 画像に隠した指示から SSH 鍵の持ち出しが 5 回中 4 回通った。審査官が昇格の要求しか見ないこと、ツールの出力を見るので注入の影響を受けることを指摘

### 3.2 設定方法

| 目的 | 方法 |
|---|---|
| CLI で入れる | `~/.codex/config.toml` に `approvals_reviewer = "auto_review"` と、対話的な承認ポリシー（`approval_policy = "on-request"`）。承認ポリシーを変えるだけでは審査官は変わらない。`approval_policy = "never"` では審査するものが無い |
| 1 回だけ入れる | `codex --approve-for-me`（`workspace-write` のサンドボックスで承認要求を自動審査に回す。`codex exec` にもある。手元の 0.162.1 で確認） |
| デスクトップアプリ | 入力欄の下の権限の設定で「Approve for me」 |
| 判定基準を差し替える（個人） | `config.toml` の `[auto_review].policy`。全文置き換えなので、既定の方針（オープンソースの `core/src/guardian/policy.md`）を写してから変える |
| 判定基準を差し替える（組織） | 管理設定の `guardian_policy_config`。個人の設定より優先 |
| 組織で縛る | 管理設定の `allowed_approval_policies`、`allowed_approvals_reviewers`、`allowed_sandbox_modes`、`allowed_permission_profiles`（`:danger-full-access` を外すと禁止）、`default_permissions` |
| 拒否をやり直す（TUI） | `/approve` で直近の拒否 1 件を選び 1 回だけ再試行。再試行も審査を通る |

設定例:

```toml
approval_policy = "on-request"
approvals_reviewer = "auto_review"

[auto_review]
# policy = """（既定の方針を写して書き換える）"""
```

### 3.3 補足: OS のサンドボックス

Codex は**既定で `workspace-write` のサンドボックスが入り**、作業フォルダの外へ書けず、**ネットワークは既定で遮断**される。`.git` は常に書き込み不可、`.aws`・`.codex`・`.agents` はディレクトリがあれば書き込み不可。ドメインの許可リスト（`network_proxy`）は任意で、既定は切り。読み取りは制限しない。`--dangerously-bypass-approvals-and-sandbox`（`--yolo`）か `danger-full-access` で外せる。execpolicy（`~/.codex/rules/` の Starlark）でコマンドの規則を書けるが、接頭辞の規則なので包んだ命令は見えない（出典 9 の issue の報告）。

## 4. Microsoft 365 Copilot と Copilot Studio

### 4.1 仕組み

Microsoft 自身の LLM がツール呼び出しを審査する仕組みは公開文書に無い。代わりに次がある。

- **リアルタイム保護**（出典 10、公開プレビュー、2025-09-08 発表）: ツールを実行する前に、エージェントの計画（ユーザーのプロンプトと会話履歴、ツール名と入力値、エージェント ID・利用者 ID・テナント ID）を外部システムへ API で送り、許可か拒否を受け取る。接続先は Microsoft Defender、他社のセキュリティ製品、自作のシステム（「bring your own protection」）。**応答の猶予は 1 秒で、応答が無ければ実行する（fail-open）**。拒否ならエージェントを止めて利用者に知らせる。Defender for Cloud Apps を接続すると、ツール呼び出しを脅威インテリジェンスと行動分析で検査する（出典 11）
- **ツール呼び出しの人の承認**（出典 12、ロードマップ RM570434）: 作成者がツールごと・エージェントごとに人の承認を要求できる。呼び出しのたびに止まり、Teams や Microsoft 365 Copilot の画面で承認・セッション中は承認・拒否を選ぶ。エージェントの指示とは独立に効く決定的なガードレール。GA 予定は 2026 年 9 月だが、ページの状態は「In development」のまま（2026-10-18 時点）
- **プロンプトインジェクションの既定の防御**: 外部由来（XPIA）と利用者由来（UPIA）の注入を既定で検出して止める（出典 10）
- **コンピューター操作の人の監督**（プレビュー、出典 13）: あいまいな場面や確認が要る場面で実行を止め、メールか画面の確認カードで人に問い合わせる。Learn の表では現行の Computer Using Agent V2 は人の監督に非対応で、退役した V1 だけが対応
- **Purview DLP**（出典 14）: 機密情報の種類を本文から検出して処理を止める。Copilot Studio のエージェントにも対応。感度ラベルの表示と暗号化は SharePoint の知識源に限る

### 4.2 設定方法

| 目的 | 方法 |
|---|---|
| リアルタイム保護を Defender で使う | Defender ポータルと Power Platform の管理者が連携して、Microsoft Entra ID のアプリケーションを作り、Microsoft 365 のアプリコネクタを接続する（出典 11 の Learn モジュール）。Agent 365 のライセンスが要るという第三者の報告がある（出典 15、未確認） |
| 自作のシステムを接続する | Copilot Studio の設定で外部システムの API を登録する（出典 10。具体的な画面は Learn を参照） |
| ツールごとに人の承認を求める | Copilot Studio のツールの設定の切り替え（RM570434。GA 後に Learn を確認） |
| 自律エージェントの設計指針 | 高リスクの操作は人の承認を求める形にし、最小権限、トリガーの真正性の確認、監査ログを置く（出典 16） |

## 5. 本プロジェクトの審査との関係

| 観点 | エージェント組み込みの審査 | ゲートウェイの審査（spec 4 章） |
|---|---|---|
| 掛かる範囲 | そのエージェントの、設定で有効にした利用者だけ | ゲートウェイを通る全エージェント・全利用者 |
| 外せるか | 利用者が設定で外せる（管理設定で縛れる） | 利用者は外せない（審査なしのキーは管理者が発行） |
| 会話を見ない審査 | 無し（3 製品とも会話を渡す） | 文脈独立審査（ツール名・引数・ツールの定義だけ） |
| 宛先の機械的な照合 | Claude Code は `autoMode.environment` の散文を分類器が解釈。Codex はネットワーク遮断と `network_proxy`。Copilot は DLP | 審査官が宛先を抜き出し、ゲートウェイが許可リストと機械的に照合 |
| 送るファイルの中身 | Claude Code の分類器は命令だけで出力を見ない。Codex はツールの出力を見る | モデル自身に指定のコマンドで読ませ、検証した中身だけを審査官に渡す |
| 届かない範囲 | | Copilot Studio のエージェントの中のツール呼び出し（Microsoft 側で実行）。ゲートウェイを通らないエージェント |

Copilot Studio のリアルタイム保護は「実行前にツールコールを外で判定する」形で、ゲートウェイの審査と同じ位置にある。ただし 1 秒の猶予と fail-open なので、この仕組みの審査官（判定の中央値 1.5〜2.2 秒、design 16.7）をそのまま接続先にすると間に合わず通る。

ゲートウェイの比較試験（design 16.7）は、Claude Code は auto mode あり・なしで流したが、Codex は Auto-review を入れていない既定の設定で流している。Auto-review あり・なしの比較は未実施（BACKLOG）。

## 6. 出典

1. [How we built Claude Code auto mode（Anthropic Engineering）](https://www.anthropic.com/engineering/claude-code-auto-mode)
2. [Claude Code: Choose a permission mode](https://code.claude.com/docs/en/permission-modes) / [Configure auto mode](https://code.claude.com/docs/en/auto-mode-config) / [Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing) / [Configure permissions](https://code.claude.com/docs/en/permissions)
3. [Auto mode is now the default in Claude Code（Anthropic）](https://claude.com/resources/articles/auto-mode-default-in-claude-code)
4. [Auto-review of agent actions without synchronous human oversight（OpenAI Alignment、2026-04-30）](https://alignment.openai.com/auto-review/)
5. [Codex: Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review)
6. [Codex: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
7. [Data Exfiltration in Codex-cli with Gpt-5.6-Sol and Auto-Review Permission Mode（第三者、2026-08-20）](https://veganmosfet.codeberg.page/posts/2026-08-20-codexcli_exfil_autoreview/)
8. [Guardian false positive crisis（第三者、2026-07-18）](https://codex.danielvaughan.com/2026/07/18/guardian-false-positive-crisis-safety-classifier-blocking-legitimate-development-codex-cli-trusted-access-workarounds/)
9. [Codex 0.162.1 の `[auto_review]` と execpolicy の限界を報告した issue（第三者）](https://github.com/kartalbas/setup-ai-core/issues/135)
10. [Strengthen agent security with real-time protection in Microsoft Copilot Studio（Microsoft、2025-09-08）](https://www.microsoft.com/en-us/copilot/blog/copilot-studio/strengthen-agent-security-with-near-real-time-protection-in-microsoft-copilot-studio/)
11. [Explore Copilot Studio AI agent protection（Microsoft Learn）](https://learn.microsoft.com/en-in/training/modules/enable-protection-copilot-studio-agents/2-explore-copilot-studio-ai-agent-protection) / [Runtime risk, real-time defense: securing AI agents（Microsoft Security Blog、2026-01-23）](https://www.microsoft.com/security/blog/2026/01/23/runtime-risk-realtime-defense-securing-ai-agents/)
12. [RM570434 Enabling makers to require human approval for tool calls（Message Center）](https://mc.merill.net/message/RM570434)
13. [Computer use FAQ（Microsoft Learn）](https://learn.microsoft.com/microsoft-copilot-studio/faqs-computer-use) / [Human supervision for computer use（Microsoft Learn）](https://learn.microsoft.com/et-ee/microsoft-copilot-studio/human-supervision-computer-use)
14. [Purview: AI in Copilot Studio（Microsoft Learn）](https://learn.microsoft.com/en-us/purview/ai-copilot-studio)
15. [Microsoft Agent 365 license enforcement July 1 2026（第三者ブログ）](https://derkvanderwoude.medium.com/microsoft-agent-365-license-enforcement-july-1-2026-security-impact-summary-47d7f02b46be)
16. [Design autonomous agent capabilities（Microsoft Learn）](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/autonomous-agents)
