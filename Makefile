# 検証環境の入口。どのターゲットも何度実行しても同じ結果になる（docs/design.md 14 章）。
SHELL := /bin/bash
S := scripts

.DEFAULT_GOAL := help

help: ## このヘルプ
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## 全段階を順に流す（何度でも可）
	@$(S)/00-preflight.sh
	@$(S)/10-cluster.sh
	@$(S)/20-images.sh
	@$(S)/30-secrets.sh
	@$(S)/40-datastores.sh
	@$(S)/50-mcp.sh
	@$(S)/60-litellm.sh
	@$(S)/70-bootstrap.sh
	@$(S)/90-clients.sh
	@$(S)/status.sh

down: ## クラスタを削除する（.env、ChatGPT のトークン、イメージの置き場は残す）
	@$(S)/down.sh

rebuild: ## クラスタを削除して作り直す（所要時間を表示）
	@start=$$(date +%s); $(MAKE) --no-print-directory down up; echo "作り直しの所要時間: $$(( $$(date +%s) - start )) 秒"

status: ## 状態を表示する
	@$(S)/status.sh

# ---- 段階ごと ----
preflight: ; @$(S)/00-preflight.sh
cluster:   ; @$(S)/10-cluster.sh
images:    ; @$(S)/20-images.sh
secrets:   ; @$(S)/30-secrets.sh
datastores: ; @$(S)/40-datastores.sh
mcp:       ; @$(S)/50-mcp.sh
litellm:   ; @$(S)/60-litellm.sh
bootstrap: ; @$(S)/70-bootstrap.sh
clients:   ; @$(S)/90-clients.sh

chatgpt-login: ## ChatGPT Plus にデバイスコードでログインする（手作業。初回だけ）
	@$(S)/80-chatgpt-login.sh

upgrade: ## LiteLLM を別の版でローリング更新する（例: make upgrade VERSION=v1.104.3）
	@test -n "$(VERSION)" || { echo "VERSION を指定してください（例: VERSION=v1.104.3）"; exit 1; }
	@$(S)/upgrade.sh $(VERSION)

clean-all: ## クラスタ、生成したクライアント設定、ビルドしたイメージを消す（.env とトークンは残す）
	@$(S)/clean-all.sh

# ---- 試験 ----
test-unit: ## ガードレールの単体テスト（LiteLLM のイメージの中で流す）
	@$(S)/test-unit.sh

test-smoke: ## 疎通確認（入口、パススルーの遮断、審査の差し替え、Claude Code の素通し）
	@tests/smoke/smoke.sh

test-idempotency: ## make up を 2 回流し、2 回目で何も変わらないことを確かめる
	@tests/idempotency.sh

test-load: ## 偽 LLM を相手にした負荷試験（k6、Docker で実行）
	@tests/load/run.sh

review-log: ## 審査の判定ログを集計する
	@tests/review-scenarios/summarize-log.sh

.PHONY: help up down rebuild status preflight cluster images secrets datastores mcp litellm bootstrap clients \
	chatgpt-login upgrade clean-all test-unit test-smoke test-idempotency test-load review-log
