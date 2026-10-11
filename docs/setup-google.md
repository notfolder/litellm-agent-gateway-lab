# Google Cloud の準備（手作業）

LiteLLM の管理画面の Google SSO と、Gmail の MCP（`google_workspace_mcp`）で使う OAuth クライアントを作ります。
ここで作った値を `.env` に入れて `./lab up` を流すと、SSO と Gmail の MCP が有効になります。
未設定のままでも、ほかの部分は構築できます。

## 1. プロジェクトと API

1. [Google Cloud コンソール](https://console.cloud.google.com/)で、検証用のプロジェクトを作る（既存のものでもよい）
2. Gmail API を有効にする

   ```bash
   gcloud services enable gmail.googleapis.com --project=<PROJECT_ID>
   ```

   gcloud が無ければ、次で入れてサインインする。gcloud を使わずに、コンソールの「API とサービス」→「ライブラリ」で Gmail API を有効にしてもよい。

   ```bash
   brew install --cask gcloud-cli
   gcloud auth login      # ブラウザが開くので、Google アカウントでサインインする
   ```

## 2. OAuth 同意画面（Google Auth Platform）

1. 「ブランディング」でアプリ名（例: `litellm-agent-gateway-lab`）とサポートメールを入れる
2. 「対象」は **外部**（個人アカウントでは「内部」を選べない）。公開ステータスは **テスト** のまま
3. 「テストユーザー」に、検証に使う Google アカウントを 2 つ追加する（`.env` の `LAB_USER1_EMAIL` と `LAB_USER2_EMAIL`）
4. 「データアクセス」で次のスコープを追加する
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/userinfo.profile`
   - `.../auth/gmail.readonly`

テストモードでは、リフレッシュトークンが 7 日で失効します（spec 6.1 の再サインインの確認に使う）。

## 3. OAuth クライアント

「クライアント」→「クライアントを作成」で、種類は **ウェブ アプリケーション**。
「承認済みのリダイレクト URI」に次の 4 つを登録する（ポートは `.env` の `LAB_HTTP_PORT`）。

| URI | 用途 |
|---|---|
| `http://localhost:8080/sso/callback` | 管理画面の Google SSO |
| `http://localhost:8080/callback` | Gmail の MCP のサインイン（LiteLLM が受ける） |
| `http://localhost:8080/ui/mcp/oauth/callback` | 管理画面から MCP に接続する場合 |
| `http://localhost:8080/lab-audit/auth/callback` | 利用料と入出力の履歴の検索アプリのサインイン（2026-10-12 追加） |

作成後に表示されるクライアント ID とシークレットを `.env` に入れる。

```bash
GOOGLE_OAUTH_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-xxxxxxxx
LAB_USER1_EMAIL=you@gmail.com
LAB_USER2_EMAIL=another@gmail.com
```

## 4. 反映

```bash
./lab up            # SSO と Gmail の MCP が有効になる
./lab test-smoke    # Gmail の MCP が未サインインで 401 を返すことまで確かめる
```

## 5. サインイン

| 対象 | 手順 |
|---|---|
| 管理画面 | `http://localhost:8080/ui` を開き、Google でログイン（`LAB_USER1_EMAIL` は管理者になる） |
| Gmail の MCP（Codex） | `clients/codex-lab mcp login gmail`。表示された URL をブラウザで開き、LiteLLM の画面で「Finish connecting」を押す。Google のサインインと権限の許可を求められたら、キーの持ち主と同じアカウントで許可する |
| Gmail の MCP（Claude Code） | `clients/claude-lab` を起動し、`/mcp` から `gmail` を選んで認証する |

- Gmail にサインインするアカウントは、LiteLLM のユーザー（キーの持ち主）と同じメールアドレスでなければ拒否されます（`oauth_identity_binding` の enforce）
- 管理画面の「MCP Servers」→「My Connections」→「Connect」は使えません。enforce の設定では、本人の確認ができない経路として LiteLLM が閉じています（403）
- `clients/codex-lab` は `mcp login` のときだけ LiteLLM のキーを外して Codex を起動します。キーが付いていると LiteLLM が 401（サインインの要求）を返さず、Codex がサインインの手順を見つけられないためです
- サインインの結果は、ユーザーごとに LiteLLM の DB に保存されます。`./lab rebuild` で DB を作り直すと消えるので、サインインをやり直します
- Codex が「Gmail にアクセスできない」と答えるようになったら、Codex のリフレッシュトークンが拒否されています（ゲートウェイは使うたびに新しいトークンに替え、使い回しを拒否する）。`clients/codex-lab mcp login gmail` でサインインし直します

## 6. 確認（2026-10-11 に実施）

| 確認 | 結果 |
|---|---|
| 管理画面に Google でログインする | ユーザー 1（`LAB_USER1_EMAIL`）が管理者として入れた |
| ユーザー 1 のキーで Gmail のツールを使う（サインイン後） | ツール 7 個が見え、ラベルの一覧を取れた |
| サインインしていないユーザー 2 のキーで同じことをする | 401 で拒否された（ユーザー 1 のサインインは使えない） |
| Codex（`clients/codex-lab`、gpt-5.6-terra）に「Gmail のラベルの数」を頼む | Gmail のツールで数え、38 と答えた。LiteLLM の Pod を入れ替えた直後でも、12 回すべてでツール 7 個が見えた |
