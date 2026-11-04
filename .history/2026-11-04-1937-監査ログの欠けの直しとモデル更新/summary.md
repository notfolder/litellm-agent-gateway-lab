# AWS（EKS の構成）の配備と試験、監査ログの欠けの直し、Bedrock のモデルの更新
更新: 2026-11-04 19:37 JST
セッション: 767eb676-85db-46f5-9fbb-99df64a24feb
ログ: ./session.md

## 概要
`LAB_TARGET=aws` を東京に初めて配備して試験を流し、見つけた不具合を直した（PR #8）。監査ログが欠ける原因（LoggingWorker の 20 秒の打ち切り）を Mac で突き止めて直し、Claude Code の Bedrock のモデルを Sonnet 4.6・Haiku 5.5・Opus 5.5 にした。AWS の環境は全部消してある。

## やったこと

### AWS の配備と試験（11/3〜11/4 未明）
- `./lab up` で CDK の 4 スタック（cert・network・data・compute）を作り、`test-smoke`（75 件 OK）、`test-idempotency`（22 項目 変化なし）、`test-audit`（31 件 OK）、`test-failover`（`litellm-drain`・`aurora-failover`・`valkey-failover` とも要求の失敗 0 件）、`test-judge`（Haiku 4.5 で 256/288）を流した。
- compute スタックだけを消して作り直し、Aurora と S3 のデータが前後で一致することを確かめた。
- 配備で見つけた不具合 12 件を直した（ESO の ARN とテンプレート、CNAME の待ち方、ALB の `/.well-known/*`、S3 のチェックサム、`./lab down` の後片付けなど。一覧は変更設計書 9.2）。
- ChatGPT のトークンを EFS の固定のアクセスポイントに置き、compute を作り直しても失わないようにした（4 つの経路を AWS で確認）。
- 最後に `./lab down` で有料の資源を全部消した。結果は `.history/2026-10-24-AWS構成/変更設計書.md` の 9.1〜9.4。

### Redis の遮断器（11/4）
- Valkey のフェイルオーバーの後、LiteLLM の Redis の遮断器が HALF_OPEN のまま戻らない Pod が出た。上流に報告した（BerriAI/litellm#49884）。直し方の案と PR の申し出が付いたが、返信はしていない。
- 遮断器を切ると Redis は約 2 分半で戻るが、監査ログが約 200 件欠けた。遮断器は有効に戻した（`a1a14a5` を `fa9c62c` で取り消し）。

### 監査ログの欠けの原因と直し（11/4）
- LiteLLM v1.104.2 のソースを読んで原因を突き止めた。LoggingWorker は、成功時の処理（全コールバック）を 1 つのコルーチンとして流し、20 秒（`LOGGING_WORKER_MAX_TIME_PER_COROUTINE`）で打ち切る。Redis を待つコールバックが先に並ぶと、監査ログの `async_log_success_event` まで届かない。
- Mac の k3d を v1.36.5 で作り直し、遮断器を有効にしたまま `valkey-pod` を流すと、直す前のコードでも打ち切り 4 件・欠け 4 件が出た。遮断器ではなく打ち切りが原因と分かった。
- 記録を `async_logging_hook`（全コールバックの記録より先に呼ばれる）に移し、`async_log_success_event` は予備にした。同じ要求を二重に記録しない。打ち切りを 2 秒に縮めて流すと、打ち切り 8 件・欠け 0 件で合格した（`e0320f6`）。
- Mac のクラスタは確認の後に `./lab down` で消し、`.env` は `LAB_TARGET=aws` に戻した。

### Bedrock のモデル（11/4）
- AWS Health の通知で、Sonnet 4.5 が 2027-04-08 に EOL になると分かった。
- Claude Code の枠を `jp.anthropic.claude-sonnet-4-6`・`jp.anthropic.claude-haiku-5-5`・`jp.anthropic.claude-opus-5-5` にした（Sonnet 5.5 は東京の `jp.` に無い）。審査官は Haiku 4.5 のまま、別の既定に分けた（`328a04e`）。

### 意図的に残したもの
- 監査ログの直しと新しいモデルは、AWS ではまだ動かしていない。新しいモデルを初めて呼ぶと、AWS Marketplace の利用の申し込みが自動で行われる。
- 失敗した要求の記録（`async_log_failure_event`）は、冒頭で呼ばれるフックが無いので従来のまま。
- Business Support+ は、EC2 の構成の検証が済むまで続ける（期限 12/28）。

## 決定事項
- 遮断器は有効のまま。上流の直しが入った版が出たら上げる（ユーザー回答 1B）。
- 上流の issue には返信せず、PR か版が出るのを待つ（ユーザー回答 1A）。
- 監査ログの欠けは PR #8 で直す（ユーザー回答 1A）。
- 新しく指定する Claude のモデルは 5.5 系にする。`jp.` に無い Sonnet は 4.6 にし、5.5 が出たら移す。審査官は Haiku 4.5 のまま（ユーザー回答 1C・2A）。
- Business Support+ は EC2 の構成の検証が済むまで続ける。
- PR #8 に 3 つのコミットを push し、説明文を更新する（ユーザー回答 1A）。

## 次にやること
- PR #8 のレビューとマージ（ユーザー）。
- AWS の EC2 の構成（`aws-ec2`）の配備と試験。そのときに監査ログの直しと 5.5 系のモデルも確かめる。
- 変更仕様書 7.2 の AWS の残り（任意の時点への復元、Pod Identity の権限、本物の Claude Code、審査官の待ち時間）。

## バックログ
- [ ] Redis の遮断器: 上流の直しの版が出たら上げて AWS で確かめる。監査ログの直しを AWS で確かめる（重要 高）
- [ ] Bedrock のモデル: 次の配備で 3 つを呼んで確かめる。`jp.` に Sonnet 5.5 が出たら移す（期限 2027-01-08）
- [ ] Business Support+ を Basic に戻す（EC2 の構成の検証の後。期限 2026-12-28）
- [ ] 変更仕様書 7.2 の AWS の残りの検証項目
- [ ] `aws-ec2` の配備と試験
- [x] (2026-11-04) Mac の k3d を v1.36.5 で作り直して試験が通ることを確かめる
