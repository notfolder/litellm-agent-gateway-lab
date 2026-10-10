# Google Cloud の準備（手作業）

LiteLLM の管理画面の Google SSO と、Gmail の MCP（`google_workspace_mcp`）で使う OAuth クライアントを作ります。
ここで作った値を `.env` に入れて `make up` を流すと、SSO と Gmail の MCP が有効になります。
未設定のままでも、ほかの部分は構築できます。

## 1. プロジェクトと API

1. [Google Cloud コンソール](https://console.cloud.google.com/)で、検証用のプロジェクトを作る（既存のものでもよい）
2. Gmail API を有効にする

   ```bash
   gcloud services enable gmail.googleapis.com --project=<PROJECT_ID>
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
「承認済みのリダイレクト URI」に次の 3 つを登録する（ポートは `.env` の `LAB_HTTP_PORT`）。

| URI | 用途 |
|---|---|
| `http://localhost:8080/sso/callback` | 管理画面の Google SSO |
| `http://localhost:8080/callback` | Gmail の MCP のサインイン（LiteLLM が受ける） |
| `http://localhost:8080/ui/mcp/oauth/callback` | 管理画面から MCP に接続する場合 |

作成後に表示されるクライアント ID とシークレットを `.env` に入れる。

```bash
GOOGLE_OAUTH_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-xxxxxxxx
LAB_USER1_EMAIL=you@gmail.com
LAB_USER2_EMAIL=another@gmail.com
```

## 4. 反映

```bash
make up            # SSO と Gmail の MCP が有効になる
make test-smoke    # Gmail の MCP が未サインインで 401 を返すことまで確かめる
```

## 5. サインイン

| クライアント | 手順 |
|---|---|
| 管理画面 | `http://localhost:8080/ui` を開き、Google でログイン（`LAB_USER1_EMAIL` は管理者になる） |
| Claude Code | `clients/claude-lab` を起動し、`/mcp` から `gmail` を選んで認証する |
| Codex | `clients/codex-lab mcp login gmail` |

Gmail にサインインするアカウントは、LiteLLM のユーザー（キーの持ち主）と同じメールアドレスでなければ拒否されます（`oauth_identity_binding` の enforce）。
