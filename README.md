# litellm-agent-gateway-lab

LiteLLM を AI エージェント向けゲートウェイとして使うための検証リポジトリです。

- **Copilot エージェント連携**: LiteLLM の MCP ゲートウェイとユーザーごとの OAuth で、Microsoft 365 Copilot（Copilot Studio のエージェント）を Claude Code や Codex から呼び出す
- **ツールコール審査**: Claude Code の auto mode を参考にした審査を、Codex など他のツールのツールコールに LiteLLM のガードレールとして掛ける
- **スケール検証**: Mac 1 台の Kubernetes（OrbStack + k3d）で、複数レプリカ構成の LiteLLM を検証する

詳細は [docs/spec.md](docs/spec.md) を参照してください。

## ステータス

設計段階。実装と検証結果は順次追加します。

## 注意

- 個人の検証環境での記録です。テナント ID やドメインなどはすべてプレースホルダーです
- Claude Code、Codex、Microsoft 365 Copilot、LiteLLM は各社の製品・商標です。本リポジトリは各社と無関係です
