# litellm-agent-gateway-lab

LiteLLM を AI エージェント向けゲートウェイとして使うための検証リポジトリです。

- **Copilot エージェント連携**: LiteLLM の MCP ゲートウェイとユーザーごとの OAuth で、Microsoft 365 Copilot（Copilot Studio のエージェント）を Claude Code や Codex から呼び出す（検証は Gmail で代用）
- **ツールコール審査**: Claude Code の auto mode を参考にした審査を、Codex など他のツールのツールコールに LiteLLM のガードレールとして掛ける
- **スケール検証**: Mac 1 台の Kubernetes（OrbStack + k3d）で、複数レプリカ構成の LiteLLM を検証する

仕様は [docs/spec.md](docs/spec.md)、詳細設計は [docs/design.md](docs/design.md) を参照してください。

## 構築

前提: macOS（Apple Silicon）、[OrbStack](https://orbstack.dev/)、Homebrew。

```bash
brew install k3d kubectl helm jq gettext
make up            # 何度実行しても同じ結果になる。初回は .env を .env.example から作る
make test-smoke    # 疎通確認（偽 LLM を使うので、利用枠を使わない）
```

手作業の準備（なくても構築は進み、該当する機能だけが無効になる）:

| 準備 | 手順 | 有効になるもの |
|---|---|---|
| Google の OAuth クライアント | [docs/setup-google.md](docs/setup-google.md) | 管理画面の SSO、Gmail の MCP |
| TypeSafe の API キー | `.env` の `TYPESAFE_API_KEY` | 審査の一次判定（Jev） |
| ChatGPT Plus のログイン | `make chatgpt-login` の後に `make up` | Codex 用のモデル、審査の二次判定 |

## よく使うコマンド

| コマンド | 内容 |
|---|---|
| `make up` | 全段階を順に流す |
| `make rebuild` | クラスタを作り直す（DB は消える。`.env`、ChatGPT のトークン、イメージの置き場は残る） |
| `make status` | 状態の表示 |
| `make upgrade VERSION=v1.x.y` | LiteLLM のローリング更新 |
| `make test-unit` / `test-smoke` / `test-idempotency` / `test-load` | 試験 |
| `make review-log` | 審査の判定ログの集計 |
| `clients/claude-lab` / `clients/codex-lab` | 検証用の LiteLLM を通して Claude Code / Codex を起動（普段の設定は変えない） |

## ステータス

構築スクリプトと審査ガードレールを実装済み。Google、TypeSafe、ChatGPT を使う部分は、手作業の準備の後に検証する。

## 注意

- 個人の検証環境での記録です。テナント ID やドメインなどはすべてプレースホルダーです
- Claude Max と ChatGPT Plus は個人の検証にだけ使い、エンドポイントを他人と共有しません（規約上の扱いは spec 5.2）
- Claude Code、Codex、Microsoft 365 Copilot、LiteLLM は各社の製品・商標です。本リポジトリは各社と無関係です
