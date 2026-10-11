# litellm-agent-gateway-lab

LiteLLM を AI エージェント向けゲートウェイとして使うための検証リポジトリです。

- **Copilot エージェント連携**: LiteLLM の MCP ゲートウェイとユーザーごとの OAuth で、Microsoft 365 Copilot（Copilot Studio のエージェント）を Claude Code や Codex から呼び出す（検証は Gmail で代用）
- **ツールコール審査**: Claude Code の auto mode を参考にした審査を、Codex など他のツールのツールコールに LiteLLM のガードレールとして掛ける
- **スケール検証**: Mac 1 台の Kubernetes（OrbStack + k3d）で、複数レプリカ構成の LiteLLM を検証する
- **冗長化と監査ログ**: PostgreSQL（CloudNativePG、3 台）と Valkey（Sentinel）をマネージドサービスを使わずに冗長化し、LLM への入出力を改ざんできない形で 90 日残す。利用者ごとの利用料（概算と按分）と入出力の履歴を検索するアプリ（`/lab-audit/`）付き

仕様は [docs/spec.md](docs/spec.md)、詳細設計は [docs/design.md](docs/design.md) を参照してください。変更ごとの仕様書と設計書は `.history/<日付>-<目的>/` にあります。

## 構築

前提: macOS（Apple Silicon）、[OrbStack](https://orbstack.dev/)（メモリの上限 12 GB）、Homebrew。

```bash
brew install uv k3d kubectl helm kubectl-cnpg
./lab up            # 何度実行しても同じ結果になる。初回は .env を .env.example から作る
./lab test-smoke    # 疎通確認（偽 LLM を使うので、利用枠を使わない）
```

構築ツール `./lab` は Python で書いてあり、uv が Python 3.12 以上と依存を自動で用意します（`./lab -h` でコマンドの一覧）。

手作業の準備（なくても構築は進み、該当する機能だけが無効になる）:

| 準備 | 手順 | 有効になるもの |
|---|---|---|
| Google の OAuth クライアント | [docs/setup-google.md](docs/setup-google.md) | 管理画面の SSO、Gmail の MCP、検索アプリ（`/lab-audit/`）のサインイン |
| TypeSafe の API キー | [console.typesafe.ai](https://console.typesafe.ai) の Billing でクレジットを追加してから、API Keys でキーを作り、`.env` の `TYPESAFE_API_KEY` に入れる | 審査の一次判定（Jev） |
| ChatGPT Plus のログイン | 先に ChatGPT の設定 →「セキュリティ」で、デバイスコードによるサインイン（Codex など）を有効にする。その後 `./lab chatgpt-login`、`./lab up` | Codex 用のモデル、審査の二次判定 |

## よく使うコマンド

| コマンド | 内容 |
|---|---|
| `./lab up` | 全段階を順に流す（段階ごとにも流せる。例: `./lab litellm`） |
| `./lab rebuild` | クラスタを作り直す（DB と監査ログは消える。`.env`、ChatGPT のトークン、イメージの置き場は残る） |
| `./lab status` | 状態の表示 |
| `./lab upgrade v1.x.y` | LiteLLM のローリング更新 |
| `./lab test-unit` / `test-smoke` / `test-idempotency` / `test-load` | 試験 |
| `./lab test-judge` | 二次判定の事例試験（本物の審査官を呼ぶので ChatGPT の利用枠を使う） |
| `./lab test-failover [--pooler direct]` / `test-restore` | DB と Redis の障害の試験 / バックアップから任意の時点に戻す試験 |
| `./lab test-audit` | 監査ログの試験（記録、整形、検索アプリ、精算、改ざんの防止、退避と再送） |
| `./lab audit-synth` / `audit-bench` / `audit-compact` | 検索の速さの試験用の合成データ / 速さの試験 / 監査ログの整形を手で流す |
| `./lab review-log` | 審査の判定ログの集計 |
| `http://localhost:8080/lab-audit/` | 利用料と入出力の履歴の検索アプリ（Google でサインイン） |
| `clients/claude-lab` / `clients/codex-lab` | 検証用の LiteLLM を通して Claude Code / Codex を起動（普段の設定は変えない） |

## ステータス

構築スクリプト、審査ガードレール、DB と Redis の冗長化、監査ログ、検索アプリを実装し、試験済み（2026-10-12。結果は design 17 章）。

## 注意

- 個人の検証環境での記録です。テナント ID やドメインなどはすべてプレースホルダーです
- Claude Max と ChatGPT Plus は個人の検証にだけ使い、エンドポイントを他人と共有しません（規約上の扱いは spec 5.2）
- Claude Code、Codex、Microsoft 365 Copilot、LiteLLM は各社の製品・商標です。本リポジトリは各社と無関係です
