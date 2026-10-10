# litellm-agent-gateway-lab 詳細設計（検証環境の構築）

最終更新: 2026-10-11（構築ツールを Python に移行）

[spec.md](spec.md) の検証環境（5 章）を、スクリプト一式で再現できるように構築するための設計です。対象は、Mac 1 台の OrbStack 上の k3d クラスタと、その上の LiteLLM、PostgreSQL、Redis、MCP サーバー、自作ガードレール、およびクライアント（Claude Code、Codex）の設定です。

---

## 1. 設計の前提（2026-10-11 に検証済み）

### 1.1 バージョン

すべてのイメージとチャートはバージョンを固定する。

| 部品 | 固定するバージョン | 確認方法 |
|---|---|---|
| OrbStack | 導入済み（Docker 29.4.0、Compose v5.1.2） | `docker info` |
| k3d | v5.9.0 | `k3d version` |
| k3s | `rancher/k3s:v1.35.5-k3s1` | 試験クラスタで起動を確認 |
| Traefik | 3.6.13（k3s v1.35.5 に同梱） | k3s のマニフェスト |
| LiteLLM | `ghcr.io/berriai/litellm-database:v1.104.2`（arm64 あり） | イメージを取得し、ソースを確認 |
| LiteLLM Helm チャート | `oci://ghcr.io/berriai/litellm-helm` 1.104.2 | `helm show values` |
| PostgreSQL | `postgres:16-alpine`（公式イメージ） | arm64 あり |
| Redis | `redis:7-alpine`（公式イメージ） | arm64 あり |
| MCP サーバー | `ghcr.io/taylorwilsdon/google_workspace_mcp:2.1.0`（arm64） | 起動を確認 |

- LiteLLM は 2026 年 3 月の PyPI 汚染（1.82.7 / 1.82.8）以降の版で、CVE-2026-42208（v1.83.7 で修正）と CVE-2026-48710（v1.84.0 で修正）も修正済みの v1.104.2 を使う
- Helm チャートに同梱の PostgreSQL / Redis（Bitnami のサブチャート）は使わない。更新が止まった `bitnamilegacy` のイメージに固定されているため。公式イメージで最小限のマニフェストを書く

### 1.2 実機で確かめたこと

| 確かめたこと | 結果 |
|---|---|
| OrbStack 上で k3d の 3 ノードクラスタが立つか | 立つ（約 90 秒）。Traefik、metrics-server、local-path-provisioner が動く |
| ガードレールに `config.yaml` の独自キーが渡るか | 渡る（`use_jev`、`jev_threshold` を `__init__` の引数で受け取れた） |
| ガードレールで経路と User-Agent を取れるか | 取れる。`call_type` が `/v1/responses` は `aresponses`、`/v1/messages` は `anthropic_messages`、Chat Completions は `acompletion`。User-Agent は `data["proxy_server_request"]["headers"]["user-agent"]` と `data["litellm_metadata"]["user_agent"]` |
| User-Agent が空のときの値 | ヘッダーは `None`、メタデータは空文字 |
| LiteLLM 組み込みのクライアント判定 | `is_claude_code_user_agent`（`claude-cli/` と `claude-code/` の前方一致）、`is_codex_user_agent`（`^codex[-_ /]`）がある。Codex のセッション ID は `session-id` / `thread-id` ヘッダー |
| `google_workspace_mcp` を外部トークンのモードで起動できるか | 起動する。トークンなしは 401、不正なトークンも 401。**`GOOGLE_OAUTH_CLIENT_SECRET` が必須**（ないと起動しない） |
| MCP サーバーが要求するスコープ | `gmail.readonly`、`userinfo.email`、`userinfo.profile`、`openid`（`WWW-Authenticate` と保護リソースのメタデータで確認） |

### 1.3 ドキュメントとソースで確かめたこと

- Codex（0.162.1）の User-Agent は `codex_cli_rs/<ver> (...)`。カスタムプロバイダでは毎ターン会話全体を送る（`store: false`）。MCP の OAuth は `codex mcp login <name>`
- Claude Code の User-Agent は `claude-cli/<ver> (external, cli)`。Claude Max のトークン（`sk-ant-oat…`）は、LiteLLM が `anthropic/*` のデプロイへ自動で転送する
- `chatgpt/` プロバイダのトークンはファイル（`auth.json`）にだけ保存される。複数レプリカでは書き込み可能な 1 つのファイルを共有する必要がある。初回ログインはデバイスコード方式で、プロキシの外で行う
- MCP の保存トークンは上流に `Authorization: Bearer` で送られる。`authorization_url` のクエリは保持される
- Decisions API は v1.104.2 で使える。Jev は TypeSafe 社がホストするモデル（`typesafe/jev-latest`、入力 100 万トークンあたり $0.042）

---

## 2. 全体構成

```mermaid
flowchart LR
    subgraph MAC["Mac mini（macOS）"]
        CC["Claude Code<br/>（bin/claude-lab）"]
        CX["Codex<br/>（CODEX_HOME=.codex-lab）"]
        BR["ブラウザ<br/>（SSO・OAuth のサインイン）"]
        subgraph ORB["OrbStack（データは /Volumes/SSD/OrbStack）"]
            subgraph K3D["k3d クラスタ litellm（server 1 + agent 2）"]
                TR["Traefik<br/>localhost:8080"]
                subgraph NS["namespace: litellm-lab"]
                    LL["LiteLLM Deployment<br/>2〜4 レプリカ（HPA）"]
                    MIG["DB 移行 Job<br/>（Helm フック）"]
                    PG[("PostgreSQL<br/>StatefulSet")]
                    RD[("Redis<br/>Deployment")]
                    WS["google_workspace_mcp<br/>Deployment"]
                end
            end
        end
    end
    CC --> TR
    CX --> TR
    BR --> TR
    TR --> LL
    LL --> PG
    LL --> RD
    MIG --> PG
    LL -- "Bearer（ユーザーの Google トークン）" --> WS
    WS --> GAPI["Google API<br/>（userinfo・Gmail）"]
    LL --> ANT["Anthropic API<br/>（Claude Max のトークンを転送）"]
    LL --> OAI["ChatGPT<br/>（chatgpt/、Plus）"]
    LL --> JEV["TypeSafe Jev<br/>（use_jev=true のとき）"]
```

---

## 3. リポジトリ構成

```
litellm-agent-gateway-lab/
├── lab                           # 入口（Python。uv で動かす）。./lab up / down / rebuild / status / upgrade / test-*
├── labtool/                      # 構築ツールの本体
│   ├── core.py                   # 定数、ログ、.env、外部コマンド、待機、kapply、HTTP
│   ├── stages.py                 # 構築の段階（preflight 〜 clients、chatgpt-login）
│   ├── render.py                 # .env から設定を作る副作用のない関数（プロキシ設定、Secret、クライアント設定）
│   ├── ops.py                    # down / status / upgrade / clean-all / test-unit
│   ├── checks.py                 # 疎通確認、冪等性、負荷、判定ログの集計
│   └── tests/                    # 構築ツールの単体テスト（unittest）
├── .env.example                  # 設定と秘密情報の一覧（値は空。.env にコピーして使う）
├── .env                          # 実際の値（.gitignore 対象）
├── k8s/
│   ├── k3d-config.yaml           # クラスタ定義（ノード数、ポート、k3s の版、ボリューム、レジストリ）
│   ├── traefik-config.yaml       # HelmChartConfig（タイムアウト）
│   ├── namespace.yaml
│   ├── ingress.yaml              # 入口（IngressRoute）とパススルー経路の遮断（Middleware）
│   ├── postgres.yaml / redis.yaml
│   ├── workspace-mcp.yaml        # google_workspace_mcp（クラスタ内のみ）
│   └── fake-llm.yaml             # 試験用の偽 LLM
├── litellm/
│   ├── Dockerfile                # 公式イメージ + ガードレール（ビルド時に単体テスト）
│   ├── values.yaml               # Helm の値
│   └── guardrails/
│       ├── tool_call_review.py   # 審査ガードレール本体（判定、差し替え、ログ）
│       ├── transcript.py         # 判定入力の抽出（形式ごと、モデルを呼ばない）
│       └── tests/                # 単体テスト（unittest）
├── clients/
│   ├── claude-lab                # Claude Code の起動ラッパー
│   └── codex-lab                 # Codex の起動ラッパー（CODEX_HOME=.codex-lab）
├── tests/
│   ├── fake-llm/server.py        # 試験用の偽 LLM（ツールコールと審査官の役）
│   ├── load/k6.js                # k6 の負荷試験のシナリオ（./lab test-load で流す）
│   └── review-scenarios/         # spec 6.2 のシナリオ試験（手順と使い捨てリポジトリ）
└── docs/
    ├── spec.md / design.md
    └── setup-google.md           # Google Cloud の手作業の手順
```

- `.env`、クライアント設定の生成物（`.codex-lab/`、`clients/out/`）、`.build/` は `.gitignore` に入れる
- 構築ツールは Python で書く（2026-10-11 に Make + bash から移した。理由は 16 章）。`./lab` の先頭に必要な Python の版（3.12 以上）と依存（PyYAML）を書いてあり、uv が自動で用意する。前提の道具は uv、Docker（OrbStack）、k3d、kubectl、helm だけ

---

## 4. 構築の流れ

```mermaid
flowchart TB
    A["./lab up"] --> P["preflight<br/>コマンド・OrbStack・SSD・.env の確認"]
    P --> C["cluster<br/>イメージの置き場 2 つ、k3d 作成（既にあれば飛ばす）<br/>Traefik のタイムアウト設定"]
    C --> I["images<br/>LiteLLM カスタムイメージをビルド<br/>自作イメージの置き場に push"]
    I --> S["secrets<br/>.env → Secret（差分だけ反映）"]
    S --> D["datastores<br/>PostgreSQL・Redis を起動し Ready 待ち"]
    D --> M["mcp<br/>偽 LLM、google_workspace_mcp を起動"]
    M --> L["litellm<br/>helm upgrade --install<br/>（DB 移行は pre-install / pre-upgrade フック）"]
    L --> B["bootstrap<br/>ユーザーと MCP 権限付きのキー"]
    B --> CL["clients<br/>Claude Code / Codex の設定を生成"]
    G["chatgpt-login（手作業。初回だけ）<br/>デバイスコードでログイン"] -. "後で ./lab up を流すと<br/>Codex 用のモデルが入る" .-> L
```

- どの段階も、何度実行しても同じ状態になるように書く（作成済みなら飛ばす、差分だけ反映、`helm upgrade --install`）
- 各段階は単独でも実行できる（`./lab litellm` で LiteLLM の段階だけなど）
- `./lab down` はクラスタを消す。保持するデータは 9 章

---

## 5. Kubernetes の構成

### 5.1 クラスタ（`k8s/k3d-config.yaml`）

| 項目 | 値 |
|---|---|
| ノード | server 1、agent 2 |
| k3s イメージ | `rancher/k3s:v1.35.5-k3s1` |
| ポート | `8080:80@loadbalancer`（Traefik の HTTP） |
| ボリューム | `${LAB_DATA_DIR}/chatgpt:/var/lib/lab/chatgpt@all`（chatgpt/ のトークン共有用。全ノードに同じホストのフォルダを見せる） |
| イメージの置き場 | クラスタとは別に k3d のレジストリを 2 つ立てる（15 章）。自作イメージ用（`k3d-litellm-registry.localhost`）と、Docker Hub の中継キャッシュ（`k3d-dockerio-cache.localhost`）。`./lab down` では消さない |

`LAB_DATA_DIR` の既定は `/Volumes/SSD/litellm-lab`。

### 5.2 Traefik（`k8s/traefik-config.yaml`）

`HelmChartConfig` で `ports.web.transport.respondingTimeouts` の `readTimeout` / `writeTimeout` / `idleTimeout` を 600 秒にする（長いストリーミングと判定待ちで切れないように）。適用後、Traefik の Deployment の起動引数に反映されたかを確かめる。

### 5.3 公開する経路とパススルーの遮断

Ingress（Traefik）で `localhost:8080` の全経路を LiteLLM に流す。ただし、プロバイダ API の素通し中継（spec 4.2）は Middleware で 403 にする。

- 遮断する接頭辞: `/anthropic`、`/openai`、`/azure`、`/bedrock`、`/vertex_ai`、`/gemini`、`/cohere`、`/mistral`、`/assemblyai`、`/langfuse`
- 実装時に、v1.104.2 のパススルー経路の一覧をソースから抽出して照合する

### 5.4 名前空間と配置

| 部品 | 種類 | レプリカ | requests / limits（メモリ） | 保存 |
|---|---|---|---|---|
| LiteLLM | Deployment（Helm） | 2〜4（HPA、CPU 60%） | 512Mi / 1Gi（構築後に実測して見直す） | なし |
| DB 移行 | Job（Helm フック） | — | — | — |
| PostgreSQL | StatefulSet | 1 | 256Mi / 512Mi | PVC（local-path、1Gi） |
| Redis | Deployment | 1 | 64Mi / 128Mi | なし（キャッシュのみ） |
| google_workspace_mcp | Deployment | 1 | 128Mi / 256Mi | なし（stateless モード） |

---

## 6. LiteLLM

### 6.1 カスタムイメージ（`litellm/Dockerfile`）

```dockerfile
FROM ghcr.io/berriai/litellm-database:v1.104.2
COPY guardrails/ /app/guardrails/
ENV PYTHONPATH=/app/guardrails
```

`./lab images` で `litellm-lab:<中身のハッシュ>` としてビルドし、自作イメージ用のレジストリに push する。Helm の `image.repository` / `image.tag` にはクラスタ内から見た名前（`k3d-litellm-registry.localhost:5000/litellm-lab`）を渡す。

### 6.2 Helm の値（`litellm/values.yaml` の要点）

| 項目 | 値 | 理由 |
|---|---|---|
| `image.repository` / `tag` | `litellm-lab` / ビルドしたタグ | 自作ガードレール入り |
| `image.pullPolicy` | `IfNotPresent` | 取り込んだイメージを使う |
| `numWorkers` | 1 | Kubernetes では 1 Pod 1 ワーカーが推奨 |
| `autoscaling` | 有効、min 2、max 4、CPU 60% | OrbStack のメモリ上限 8 GB に収める（12 章） |
| `masterkeySecretName` | `litellm-secrets` | Secret から |
| `environmentSecrets` | `litellm-secrets` | 下の表の環境変数 |
| `db.useExisting` | true（`postgres` Service を指す） | 同梱 DB を使わない |
| `db.deployStandalone` / `redis.enabled` | false / false | 同上 |
| `migrationJob.hooks.helm.enabled` | true（argocd は false） | 素の `helm` で移行を走らせる |
| `envVars` | `LITELLM_MODE=PRODUCTION`、`DISABLE_SCHEMA_UPDATE=true`、`LITELLM_LOG=INFO`、`PROXY_BASE_URL=http://localhost:8080`、`CHATGPT_TOKEN_DIR=/var/lib/lab/chatgpt` | spec 5.1、本番の推奨 |
| `volumes` / `volumeMounts` | hostPath `/var/lib/lab/chatgpt` | chatgpt/ のトークン共有（5.1） |
| `ingress` | 無効（自前の Ingress と Middleware を使う） | 5.3 |

### 6.3 プロキシの設定（`litellm/config.yaml`）

```yaml
model_list:
  # Claude Code（Claude Max のトークンを転送。api_key は書かない）
  - model_name: "anthropic/*"
    litellm_params:
      model: "anthropic/*"
  # Codex（ChatGPT Plus）。モデル名は契約後に確定
  - model_name: "<codex 用のモデル名>"
    litellm_params:
      model: "chatgpt/<モデル>"
  # 一次判定
  - model_name: jev
    litellm_params:
      model: typesafe/jev-latest
      api_key: os.environ/TYPESAFE_API_KEY
  # 二次判定（ChatGPT Plus の小さめのモデル。モデル名は 13 章で確定）
  - model_name: review-judge
    litellm_params:
      model: "chatgpt/<小さめのモデル>"

guardrails:
  - guardrail_name: tool-call-review
    litellm_params:
      guardrail: tool_call_review.ToolCallReview
      mode: [post_call]
      default_on: true
      use_jev: true
      jev_model: jev
      jev_threshold: 0.2
      judge_model: review-judge

mcp_servers:
  gmail:
    url: "http://workspace-mcp.litellm-lab.svc.cluster.local:8000/mcp"
    transport: "http"
    auth_type: "oauth2"
    oauth2_flow: "authorization_code"
    client_id: os.environ/GOOGLE_OAUTH_CLIENT_ID
    client_secret: os.environ/GOOGLE_OAUTH_CLIENT_SECRET
    authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent"
    token_url: "https://oauth2.googleapis.com/token"
    scopes:
      - "openid"
      - "https://www.googleapis.com/auth/userinfo.email"
      - "https://www.googleapis.com/auth/userinfo.profile"
      - "https://www.googleapis.com/auth/gmail.readonly"
    oauth_identity_binding:
      mode: enforce
      issuer: "https://accounts.google.com"
      audiences: [os.environ/GOOGLE_OAUTH_CLIENT_ID]   # 実装時に env 参照が使えるか確認
      principal_claim: email
      caller_field: user_email
      require_email_verified: true

router_settings:
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  redis_password: os.environ/REDIS_PASSWORD
  routing_strategy: simple-shuffle
  disable_cooldowns: true          # Claude Max のトークン切れで他人が止まらないように

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  database_connection_pool_limit: 5
  require_key_mcp_access_defined: true
  proxy_batch_write_at: 60
```

- `oauth_identity_binding` で、Gmail にサインインした Google アカウントのメールと、LiteLLM のユーザー（Google SSO）のメールが一致しないと 403 にする。これで「他人の Gmail を自分のキーで読む」経路を閉じる
- `database_connection_pool_limit: 5` は、PostgreSQL の上限 100 ÷（最大 Pod 数 4 × ワーカー 1）に、DB 移行の Job と管理用の接続の余裕を持たせた値
- 二次判定は ChatGPT Plus を使うため、Codex と同じ利用上限を分け合う。上限に当たるようになったら、`review-judge` の `model` を Anthropic API などに替える（設定 1 行）

### 6.4 環境変数（Secret `litellm-secrets`）

| 変数 | 内容 | 出所 |
|---|---|---|
| `LITELLM_MASTER_KEY` | 管理用キー（`sk-` で始まる） | `./lab secrets` が未設定なら生成して `.env` に保存 |
| `LITELLM_SALT_KEY` | DB 内の秘密の暗号化キー（後から変えない） | 同上 |
| `DATABASE_URL` | PostgreSQL の接続先 | `.env` のパスワードから組み立て |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | Redis | 同上 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | 管理画面の Google SSO | Google Cloud（7 章） |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | Gmail の MCP 用 OAuth | 同上（SSO と同じクライアントでよい） |
| `TYPESAFE_API_KEY` | Jev | TypeSafe（作成済み） |
| `PROXY_ADMIN_ID` | 管理者にする SSO ユーザー | 自分の Google アカウント |
| `ALLOWED_EMAIL_DOMAINS` | SSO を許可するドメイン | `gmail.com`（個人アカウントのため、実質は同意画面のテストユーザーで絞る） |

---

## 7. Google Cloud の設定

OAuth クライアントは 1 つ（ウェブ アプリケーション）にして、SSO と Gmail の MCP で共用する。

| 項目 | 値 |
|---|---|
| 有効にする API | Gmail API |
| 同意画面 | 外部、テストモード。テストユーザーに検証用の Google アカウント 2 つ |
| スコープ | `openid`、`userinfo.email`、`userinfo.profile`、`gmail.readonly` |
| リダイレクト URI | `http://localhost:8080/sso/callback`（SSO）、`http://localhost:8080/callback`（MCP）、`http://localhost:8080/ui/mcp/oauth/callback`（管理画面から接続する場合） |

この手順は手作業。`docs/` に手順書を置き、`./lab preflight` は `.env` に値があるかだけを確かめる。

---

## 8. 審査ガードレール（`litellm/guardrails/`）

### 8.1 処理の流れ

```mermaid
flowchart TB
    R["応答（ストリーミング / 非ストリーミング）"] --> K{"call_type が anthropic_messages<br/>かつ User-Agent が Claude Code?"}
    K -- はい --> PASS["素通し（件数をログ）"]
    K -- いいえ --> T{"応答にツールコールがある?"}
    T -- ない --> OUT["そのまま返す"]
    T -- ある --> X["判定入力を作る<br/>（ユーザー発言＋ツールコール本体）"]
    X --> J{"use_jev?"}
    J -- true --> J1["Jev（adecisions）<br/>ブロック確率 ≥ 閾値?"]
    J1 -- 未満 --> ALLOW["許可"]
    J1 -- 以上 --> J2["二次判定（judge_model）<br/>理由付き"]
    J -- false --> J2
    J2 -- 許可 --> ALLOW
    J2 -- 拒否 --> DENY["ツールコールを<br/>「拒否: 理由」のテキストに差し替え"]
    ALLOW --> LOG["判定ログ（JSON 1 行）"]
    DENY --> LOG
```

### 8.2 部品

| 部品 | 内容 |
|---|---|
| `ToolCallReview.__init__` | `use_jev`、`jev_model`、`jev_threshold`、`judge_model`、`fail_mode`（判定 API の失敗時に拒否するか通すか。既定は拒否）を受け取る |
| `async_post_call_success_hook` | 非ストリーミングの応答を判定する |
| `async_post_call_streaming_iterator_hook` | ストリーミングの応答を判定する。テキストの差分はそのまま流し、ツールコールの項目（Responses API の `function_call` / `custom_tool_call` / `local_shell_call`、Chat Completions の `tool_calls`、Anthropic の `tool_use`）の部分だけ溜めて、完了時に判定してから流すか差し替える |
| `transcript.py` | 3 形式（Responses、Chat Completions、Anthropic Messages）の会話から、ユーザー発言とツールコール本体だけを抜き出す。アシスタントの文章、推論、ツールの結果は捨てる |
| クライアント判定 | LiteLLM 組み込みの `is_claude_code_user_agent`、`is_codex_user_agent` を使う |
| 判定ログ | 標準出力に JSON 1 行: 時刻、クライアント（UA 種別）、セッション ID、キーの別名、ツール名、引数の冒頭 200 文字、`use_jev`、Jev の確率とカテゴリ、二次判定の結果と理由、判定にかかった時間、最終判定 |

### 8.3 テスト

- `transcript.py` と判定分岐は、モデルを呼ばずに単体テストできるように分ける（pytest、カスタムイメージの中で実行）
- ストリーミングの差し替えは、LiteLLM の `mock_response` で固定のツールコールを返すモデルを使って結合テストする（実装時に、`mock_response` でツールコールを返せるかを確かめる）

---

## 9. データの保持

| データ | 置き場所 | `./lab down`（クラスタ削除）で |
|---|---|---|
| Docker のイメージ・ボリューム | `/Volumes/SSD/OrbStack`（OrbStack） | 残る |
| PostgreSQL（ユーザー、キー、MCP の OAuth トークン、利用量） | クラスタ内の PVC | **消える**（作り直すたびにまっさらから試すため）。ユーザーとキーは `./lab bootstrap` が同じ値で作り直す。Gmail のサインインだけはやり直す |
| chatgpt/ のトークン（`auth.json`） | `${LAB_DATA_DIR}/chatgpt`（SSD） | 残る |
| `.env`（秘密情報） | リポジトリ直下（Git 管理外） | 残る |

---

## 10. クライアントの設定（`./lab clients`）

利用者の普段の設定（`~/.claude`、`~/.codex`）は書き換えない。

| クライアント | 方法 |
|---|---|
| Claude Code | `clients/claude-lab` で `ANTHROPIC_BASE_URL=http://localhost:8080` と `ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer <キー>"` を設定し、`--mcp-config clients/out/mcp.json` を付けて `claude` を起動する |
| Codex | `CODEX_HOME=<repo>/.codex-lab` に `config.toml` を生成する（モデルプロバイダ `litellm`、`wire_api = "responses"`、MCP の `gmail`）。`codex mcp login gmail` で初回サインイン |

キーは `./lab bootstrap` が利用者ごとに 2 本（Claude Code 用、Codex 用）作る。どちらも `object_permission.mcp_servers: ["gmail"]` を付ける。キーの値は `.env` に保存した固定値を使う（`/key/generate` の `key` 指定）ので、クラスタを作り直してもクライアントの設定は変わらない。

---

## 11. spec の検証項目との対応

| spec の項目 | 使うもの |
|---|---|
| 6.1 認証・Copilot 連携 | `tests/smoke/`、2 アカウントでの手動確認 |
| 6.2 ツールコール審査 | `tests/review-scenarios/`（使い捨てリポジトリとローカルの bare リポジトリを作り、シナリオを再現）、判定ログの集計スクリプト |
| 6.3 スケール | `tests/load/`（k6）、`kubectl scale` と HPA の観察、`./lab upgrade`（バージョンを上げてローリング更新） |

---

## 12. 決定事項（2026-10-11 のヒアリング）

| 項目 | 決定 | 反映先 |
|---|---|---|
| 二次判定の LLM | ChatGPT Plus（`chatgpt/` の小さめのモデル）。上限に当たったら Anthropic API などに切り替える | 6.3 |
| Jev | 使う（TypeSafe のアカウントは作成済み）。既定は `use_jev: true` | 6.3、6.4 |
| メモリと台数 | OrbStack は 8 GB のまま。LiteLLM は 2〜4 台 | 5.4、6.2、spec 5.1 |
| クライアントの設定 | 普段の設定から分ける（`clients/claude-lab`、`CODEX_HOME`） | 10 章 |
| DB の保持 | 残さない。クラスタを消すと DB も消える | 9 章 |
| 2 つ目の Google アカウント | 手持ちのアカウントを使う | 7 章 |
| 構築スクリプト | 冪等性を重視する。検証で何度も作り直す前提 | 14 章 |

## 13. 実装時に確かめること

- 事前に作ったユーザー（メールアドレス付き）に、Google SSO でログインしたユーザーが結び付くか（ソースではユーザー ID とメールで既存ユーザーを探しているので、結び付く見込み）
- 二次判定と Codex に使う `chatgpt/` のモデル名

- ストリーミングの Responses API で、ツールコールの項目がどのイベントの並びで届くか（溜めて差し替える処理の設計に直結）
- `mock_response` でツールコールを含む応答を作れるか
- `oauth_identity_binding.audiences` に環境変数の参照を書けるか
- Traefik の HelmChartConfig のタイムアウトが反映されるか
- `chatgpt/` で Codex が使えるモデル名と、Codex から LiteLLM 経由で動くか
- 複数レプリカが 1 つの `auth.json` を共有したときに、トークンの更新が競合しないか
- `google_workspace_mcp` に LiteLLM から実際の Google トークンを渡して Gmail を読めるか

---

## 14. 冪等性の設計

検証では環境を何度も作り直すため、どのコマンドも「何度流しても同じ結果になる」ことを最優先にする。

### 14.1 原則

- **状態を見てから動く。** 作る前に「もうあるか」を確かめ、あれば飛ばすか差分だけ反映する
- **宣言的な道具を使う。** k8s は `kubectl apply`、Helm は `helm upgrade --install`、k3d は設定ファイル（`k3d-config.yaml`）から作る
- **途中で失敗しても、もう一度流せば続きから進む。** 各段階は前の段階の完了を前提にせず、必要な状態を自分で確かめる
- **値は固定する。** 生成する秘密情報やキーは最初に 1 回だけ作って `.env` に保存し、以後は同じ値を使う
- **待つときは状態で待つ。** `sleep` ではなく `kubectl wait`、`kubectl rollout status`、ヘルスチェックの URL を、上限時間つきで繰り返し確かめる

### 14.2 段階ごとの冪等化

| 段階 | 「もうある」の判定 | 2 回目以降の動き |
|---|---|---|
| preflight | — | 毎回確かめるだけ（変更しない） |
| cluster | `k3d cluster list -o json` にクラスタがあり、全ノードが Ready | 飛ばす。設定（ノード数、k3s の版、ポート）が `k3d-config.yaml` と違えば、作り直しを促して止まる |
| cluster（Traefik の設定） | `kubectl apply` | 差分だけ反映 |
| images | イメージのタグを `litellm/` 以下の内容のハッシュにし、同じタグがローカルにあればビルドを飛ばす。レジストリにそのタグのマニフェストがあれば push も飛ばす | 中身が変わったときだけビルドと push |
| secrets | `.env` の各値。空なら生成して `.env` に書き足す（既存の値は決して上書きしない） | Secret のマニフェストを作り、`kubectl diff` で差分があるときだけ apply する |
| datastores | `kubectl apply` | 差分だけ反映。`rollout status` で Ready を待つ |
| mcp | `kubectl apply` | 同上。`/health` を待つ |
| litellm | 渡す値のハッシュをリリースの値（`labValuesHash`）に入れておき、同じなら helm を流さない | 値が変わったときだけ `helm upgrade --install --wait`。DB 移行の Job はフックで毎回走るが、移行自体が冪等 |
| bootstrap | ユーザーは `/user/info`、キーは `/key/info` で存在を確かめる | なければ `.env` の固定値で作る。あれば権限（MCP サーバー）だけ `/key/update` で揃える |
| chatgpt-login | `${LAB_DATA_DIR}/chatgpt/auth.json` にトークンがある | 飛ばす（クラスタを作り直しても残る） |
| clients | 生成物を一時ファイルに書き、内容が変わったときだけ置き換える | 差分がなければ何もしない |

### 14.3 作り直しの入口

| コマンド | 動き | 残るもの |
|---|---|---|
| `./lab up` | 全段階を順に流す（何度でも可） | — |
| `./lab down` | クラスタを削除する | `.env`、chatgpt のトークン、Docker のイメージ |
| `./lab rebuild` | `./lab down` の後に `./lab up` | 同上 |
| `./lab upgrade <版>` | LiteLLM のイメージを指定の版で作り直し、`helm upgrade` でローリング更新する（spec 6.3 の無停止更新の試験用） | すべて |
| `./lab clean-all` | `./lab down` に加えて、生成したクライアント設定とカスタムイメージを消す。確認のプロンプトを出す | `.env`、chatgpt のトークン |

- `.env` と chatgpt のトークンは、どのコマンドでも消さない（消すと Google の再設定や ChatGPT の再ログインが要るため）。消したいときは手で消す
- `./lab rebuild` の所要時間を計測して記録し、作り直しの負担を把握する

### 14.4 冪等性の試験

- `./lab up` を 2 回続けて流し、2 回目で何も変わらないこと（Helm のリビジョンが増えない、Pod が再起動しない）を確かめる
- 各段階の途中で止め（`Ctrl-C`）、もう一度 `./lab up` で最後まで進むことを確かめる
- `./lab rebuild` の後、生成済みのクライアント設定のまま Claude Code と Codex がつながることを確かめる（キーの固定値の確認）

---

## 15. 実装で分かったこと（2026-10-11）

実装と試験の中で分かり、設計を変えたことをまとめる。

| 分かったこと | 対応 |
|---|---|
| `k3d image import` は、複数アーキテクチャのイメージ（`docker save` に arm64 以外の参照が残る）で「content digest not found」になる。しかも最後に「成功」と表示する | 取り込みをやめ、レジストリ方式にした。自作イメージは `docker push --platform linux/arm64` で入れ、外部イメージは Docker Hub の中継キャッシュ経由でノードが取る。キャッシュはクラスタを消しても残るので、作り直しの 2 回目以降が速い |
| `chatgpt/` のモデルをトークン無しで設定に入れると、LiteLLM が起動の途中でデバイスコードのログインを始めて止まる（起動の確認に通らず、再起動を繰り返す）。途中で止まると、要求時刻だけの `auth.json` が書き残される | トークン（`refresh_token` か `access_token`）があるときだけ、Codex 用のモデルと二次判定の `chatgpt/` を設定に入れる。ログインは `./lab chatgpt-login` で、クラスタの外の 1 回限りのコンテナで行う |
| post_call のフックに渡る `data` には `call_type` が入っていない | 形式（Responses / Chat Completions / Anthropic Messages）は、`user_api_key_dict.request_route` などの経路から判定する |
| ストリーミングでガードレールに届く形は形式ごとに違う。Responses はイベントのオブジェクト、Chat Completions は `ModelResponseStream`、Anthropic Messages は SSE のバイト列 | 形式ごとの処理（`ResponsesStream` / `ChatStream` / `AnthropicStream`）に分けた。ツールコールの始まりまでは素通しで流し、そこから先を溜めて判定する |
| Helm チャートは、`migrationJob` を有効にすると本体の Pod に `DISABLE_SCHEMA_UPDATE=true` を自分で入れる。`envVars` で重ねて指定すると、重複でインストールが失敗する | `envVars` から外した |
| `helm --wait` を途中で止めると、リリースが pending のまま残り、次の helm が失敗する | `./lab litellm` の冒頭で、pending-install なら削除、pending-upgrade なら直前の版に戻す（戻せなければ削除）、failed なら入れ直す |
| `kubectl apply` は、中身が同じでも `configured` と出ることがある | `kubectl diff` で差分があるときだけ apply する（`kapply`）。冪等性の試験で「変化なし」を判定できるようにするため |
| Kubernetes は Service ごとに `<名前>_PORT=tcp://…` の環境変数を Pod に入れる。google_workspace_mcp は `WORKSPACE_MCP_PORT` を自分の待ち受けポートとして読むので、Service 名が `workspace-mcp` だと起動に失敗する（Docker 単体での起動確認では起きなかった） | Pod に `enableServiceLinks: false` を付けて、Service の環境変数を入れない |
| 管理画面の「Connect」で Gmail にサインインすると、Google のトークンの交換までは通るが、保存の段階で 403（`oauth_identity_binding_enforced`）になる。enforce の設定では、本人を確かめられない保存の経路を LiteLLM が閉じている（仕様） | サインインは MCP クライアント（Codex の `mcp login`、Claude Code の `/mcp`）からゲートウェイの OAuth の手順で行う。`docs/setup-google.md` の 5 章 |
| Codex の設定で MCP の要求に LiteLLM のキー（`x-litellm-api-key`）を付けていると、`codex mcp login` が「OAuth metadata discovery failed」で失敗する。キーがあると `GET /gmail/mcp` が 401 ではなく 200（SSE）を返し、Codex がそれをメタデータとして読もうとするため | `clients/codex-lab` は `mcp login` のときだけキーを外す |
| サインイン後、ユーザー 1 のキーでは Gmail のツールが使え、サインインしていないユーザー 2 のキーは 401 になった（ユーザーごとのトークンの分離） | 疎通確認の「未サインインなら 401」は、サインインしないマスターキーで確かめるように変えた |
| Codex（0.162.1）を試験用の偽 LLM（モデル名 `fake-tools`）で動かすと、Gmail の MCP のツールがモデルに渡されない（`codex mcp list` では enabled / OAuth）。未知のモデル名でのツールの扱いが原因とみているが、未確定 | ChatGPT のログインの後、本物のモデルで確かめる |
| ChatGPT のデバイスコードでのログインは、アカウント側の設定で有効にしないと、コードを入力した後に「ChatGPT のセキュリティ設定で、Codex、Excel、PowerPoint、Word のデバイスコードによるサインインを有効にしてから、`codex login --device-auth` をもう一度実行してください」と出て止まる（2026-10-11 に確認）。また、LiteLLM のログイン処理は URL とコードを表示するだけでブラウザを開かず、コードを要求した時刻だけが `auth.json` に残っていると 5 分間は新しいコードを出さずに黙って待つ | ChatGPT の設定 →「セキュリティ」で、デバイスコードによるサインインを有効にしてから `./lab chatgpt-login` を実行する（文中の `codex login --device-auth` は Codex 本体向けの案内なので、この環境では `./lab chatgpt-login` をやり直す）。`./lab chatgpt-login` は、トークンの無い `auth.json` を消してから始め、表示された URL をブラウザで開く |
| LiteLLM は MCP の経路（`/<サーバー名>/mcp`）へのセッション無しの GET に、終わらないイベントストリーム（200）を返す。Codex（0.162.1）は起動時にこの GET を送って応答を待ち続け、MCP サーバーを使えなくなる | 入口（Traefik）で、この GET だけを POST 専用の経路に置き換えて 405 を返す（`k8s/ingress.yaml`）。MCP の仕様では、サーバーはこの GET に 405 を返してよい。直した後、Codex は MCP につながり、ツール 7 個の一覧を取れた |
| Codex は MCP の一覧を取れるようになったが、本物のモデル（gpt-5.6-terra）で「Gmail のラベルの数」を頼むと「Gmail にアクセスできない」と答える。Codex はコードを実行する形（`exec` のツール）でツールを探しており、Gmail のツールが見つかっていない様子。原因は未確定 | 調査中 |
| `chatgpt/` で使えるモデル名は、ChatGPT のアカウントで決まる。`gpt-5.3-codex` は「ChatGPT のアカウントでは使えない」と拒否された。Codex 本体のモデル一覧（`~/.codex/models_cache.json`）にある名前なら使える | `.env` の既定を `LAB_CODEX_MODEL=gpt-5.6-terra`、`LAB_JUDGE_MODEL=gpt-5.6-luna` にした |
| 二次判定が本物のモデルになると判定が揺れ、疎通確認（偽 LLM で危険な操作が止まるか）が決まらなくなる | ガードレールに `test_model` / `test_judge_model` を足し、試験用のモデル（`fake-tools`）への要求は偽 LLM の審査官だけで判定する（Jev も使わない） |
| 本物の二次判定が、Codex がツールを探す操作（MCP のリソースの一覧）を「頼まれたツールではない」として止めることがあった（誤った拒否） | 審査のシナリオ試験（spec 6.2）で、誤った拒否の率として測る |
| ローリング更新の直後に、終了中の Pod に当たって入口のヘルスチェックが一時的に失敗し、bootstrap の段階が止まった | bootstrap の接続の確認を、1 回ではなく最大 60 秒待つようにした |
| TypeSafe は、組織の残高（クレジット）が無いと API キーを作れない | 手作業の準備に「Billing でクレジットを追加してからキーを作る」を足した。キーを入れた後、Codex のツールコールが Jev の一次判定（約 0.3 秒、確率 0.03 と 0.16）で判定されることを確かめた |
| OrbStack が `~/Documents` 以下をコンテナに見せるには、macOS の「書類フォルダへのアクセス」の許可が要る。許可を求める画面が出ている間、`docker run` は止まる | 2026-10-11 に許可済み。構築スクリプトは `~/Documents` をコンテナに見せない（ビルドは Docker のビルドコンテキストで送る） |

### 試験用の偽 LLM（`tests/fake-llm`）

ChatGPT や Claude の利用枠を使わずに審査を試すため、OpenAI 互換の偽 LLM を足した（クラスタ内の `fake-llm`、モデル名 `fake-tools`）。

- ユーザーの発言に `TOOL:<名前>:<引数の JSON>` があれば、そのツールコールを返す（Chat Completions と Responses API、ストリーミングあり・なし）
- 審査の依頼（`[[tool-call-review]]` の印が付いたもの）には、危険な語（`--delete`、`rm -rf` など）を含むかどうかで block / allow の JSON を返す
- ChatGPT のトークンが無いあいだは、二次判定（`review-judge`）もこの偽 LLM を使う

### 審査の試験の結果（2026-10-11、偽 LLM で実施）

3 形式 × ストリーミングあり・なしのすべてで、危険なツールコールは「拒否: 理由」に差し替わり、安全なものはそのまま通った。
Claude Code の User-Agent の Anthropic 形式は審査されずに素通りし、User-Agent が空のものは審査された。

---

## 16. 構築ツールを Python にした理由（2026-10-11）

最初は Make + bash で実装し、試験がすべて通った版をコミットした（`7880fa8`）。そのうえで、Python の 1 本の CLI（`./lab`）に書き直した。

| 観点 | Make + bash | Python（`./lab`） |
|---|---|---|
| Make の依存関係の機能 | 作る対象がファイルではなくクラスタの状態なので、ほぼ使えない。各ターゲットは順に流すだけのメニューになる | 要らない |
| 冪等性の判定（状態を読み、差だけを直す） | `kubectl -o json` や API の応答を jq で読む。文字列の比較が多い | JSON を辞書として扱える |
| 待機、リトライ、エラー処理 | `set -e` の落とし穴（パイプやサブシェル）に注意し続ける | 例外で書ける |
| 設定の生成 | envsubst のテンプレートと Python が混在 | Python だけ（副作用のない関数にして単体テストする） |
| 前提の道具 | make（macOS は 2006 年の 3.81）、jq、gettext | uv（Python 3.12 以上と PyYAML を自動で用意する） |

- k8s のマニフェスト（`k8s/`）は YAML のまま残し、`${名前}` の置き換えは Python の `string.Template` で行う。渡していない名前が残っていればエラーにする
- 外部コマンド（k3d、kubectl、helm、docker）は `subprocess` で呼ぶ。ツールのログは標準エラーに流す
- 移行の確認（2026-10-11）: 生成するプロキシ設定が bash 版と同じ内容であること、単体テスト（構築ツール 17 件、ガードレール 9 件）、疎通確認 19 項目、冪等性の試験（2 回目は 3 秒で変化なし）、まっさらからの作り直し（559 秒。bash 版は 514 秒で、差はイメージの展開など毎回ばらつく待ち時間の範囲）と、その後の疎通確認 19 項目

