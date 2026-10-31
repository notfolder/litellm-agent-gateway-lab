# 担当部署への依頼事項（VM 3 台構成、本番、AWS）

`LAB_TARGET=aws-ec2`、`LAB_INFRA=provided` で動かすために、クラウドの資源を持つ部署に用意してもらうものの一覧。
GCP（`gcp-vm`）で動かす場合の対応は末尾の「GCP の場合」。設計の根拠は [spec.md](spec.md) 12 章、[design.md](design.md) 21 章、
経緯は `.history/2026-10-31-内部LBとVM側の防御とZabbix/`、`.history/2026-10-31-静的キーを2つに/`、`.history/2026-10-31-台の増減とLiteLLMのメモリ/`。
IAM ポリシーの JSON とライフサイクルの規則は `./lab provider-requirements` が `.env` のバケット名を埋めて出す。

## 1. VM 3 台（k3s を動かす台）

| 項目 | 依頼内容 |
|---|---|
| 台数と OS | 3 台。Ubuntu 24.04 LTS（arm64 でも amd64 でも可）。ディスク 40 GiB 以上 |
| CPU とメモリ | 1 台あたり **CPU 4 個・メモリ 32 GB**（例 AWS の r7g.xlarge / r8g.xlarge）。LiteLLM 1 つが 8 GB まで使う前提で、1 台に 3 つ、3 台で 9 つ置ける。1 台が止まっても 6 つ残る |
| 配置 | 同じ VPC の中。AZ は分けてよい（内部ロードバランサが全 AZ に流す） |
| IP | 固定のプライベート IP。公開 IP は不要（構築ツールは踏み台を経由して入る） |
| ログイン | `sudo` できる利用者と SSH の鍵（こちらが作った公開鍵を登録してもらう） |
| クラウドの権限 | **不要**。インスタンスロール（IAM ロール）は付けない。VM はクラウドの API を呼ばない |
| 外向きの通信 | Zscaler（明示プロキシ）の URL と、Zscaler の証明書（CA、PEM 形式）。VM・コンテナ・構築ツールはこの中継を通る |
| 受け取る物 | 3 台のインスタンス ID、プライベート IP、SSH の利用者名 |

### 1.1 足す台（処理量を増やすとき）

LiteLLM と入口だけを動かす台を、必要なときに足す。DB は持たないので、消しても失うデータは無い。構築ツールは加えるときに台の CPU とメモリを読み、
LiteLLM を 1 つも置けない台（OS と k3s 本体の分 CPU 0.25 個・メモリ 1 GiB を除いて、LiteLLM 1 つの CPU 0.25 個とメモリの上限が入らない台）は加えない。

| 依頼 | 内容 |
|---|---|
| 足す | 1 章と同じ条件の VM を作り、内部ロードバランサの 2 つのターゲットグループ（443 と 80）に登録する。CPU とメモリは 1 章と同じ（CPU 4 個・メモリ 32 GB）を基本とし、違ってもよい |
| 受け取る物 | インスタンス ID（`.env` の `LAB_VM_WORKER_NODES` に書く）、プライベート IP |
| 減らす | こちらが `.env` の一覧から外して `./lab up` を流し、クラスタから外したと連絡した後に、ターゲットグループからの登録の解除と VM の削除をする |

## 2. 踏み台 1 台

管理画面（LiteLLM の `/ui`、監査ログの検索アプリ）、DB への接続、VM 3 台への SSH は、すべてこの踏み台からだけ通す。

| 項目 | 依頼内容 |
|---|---|
| 台 | 操作者が SSH できるホスト 1 台（既存の踏み台でよい）。同じ VPC の中 |
| 受け取る物 | SSH の宛先（ホスト名か IP）と利用者名、踏み台のプライベート IP |

## 3. 内部ロードバランサ（利用者の入口）

| 項目 | 依頼内容 |
|---|---|
| 種類 | Network Load Balancer、**内部**（internal）。VPC の内側からだけ届く |
| リスナー | TCP 443 と TCP 80 の 2 つ。TLS は終端しない（素通し。証明書は VM 側の Traefik が持つ） |
| ターゲット | VM 3 台と足す台（インスタンス指定）。443 と 80 のターゲットグループ 2 つ |
| ヘルスチェック | TCP 80。間隔 10 秒、2 回失敗で外す、2 回成功で戻す |
| IP | 固定のプライベート IP（各サブネットに 1 つ） |
| 送信元 IP | 保持する（既定）。Traefik が利用者の IP を見て、管理系の経路を踏み台だけに絞る |
| 受け取る物 | ロードバランサの ARN と内部 IP |

## 4. ネットワーク（セキュリティグループなど）

守りは VM 側の nftables で行うので、クラウドのファイアウォールは最小で足りる。

| 通信 | 送信元 → 宛先 | ポート |
|---|---|---|
| 操作者 → 踏み台 | 操作者の回線 → 踏み台 | 22 |
| 踏み台 → VM | 踏み台 → 全台（3 台と足す台） | 22 |
| 利用者 → 入口 | VPC の中 → ロードバランサ → 全台 | 80、443 |
| ヘルスチェック | ロードバランサ → 全台 | 80 |
| VM の間 | 全台の間 | すべて |
| 監視 | 全台と踏み台 → Zabbix サーバー | 10051。Zabbix サーバー → 全台と踏み台は 10050 |
| 外向き | 全台と踏み台 → Zscaler | Zscaler のポート |

受け取る物: VPC の CIDR（VM の nftables が 80/443 を通す範囲）。

## 5. S3 バケット 1 つ

| 項目 | 依頼内容 | 理由 |
|---|---|---|
| 作成 | 東京リージョン。名前は任意。**作成時に「オブジェクトロック」を有効化**（バージョニングも有効になる） | 監査ログの原本と閲覧記録を、期限まで消せない・書き換えられない形で残す |
| 既定の保持 | **付けない** | 保持は書く側がファイル単位で付ける。既定を付けると上書きが要るイメージ置き場まで消せなくなる |
| 保持のモード | COMPLIANCE（管理者でも期限前に消せない。2026-10-11 の決定） | 原本 90 日、閲覧と管理操作の記録 90 日 |
| パブリックアクセスの遮断 | 4 項目すべて有効 | 公開しない |
| 暗号化 | 既定（S3 管理の鍵）でよい。KMS を使うなら鍵の利用権限を下の 2 ユーザーにも付ける | |
| ライフサイクル | 下の規則。無くても動くが、期限後の自動削除が無くなる | 期限の過ぎた原本を消し、容量を抑える |
| 受け取る物 | バケット名 | |

バケットの中はディレクトリ（ファイル名の先頭の文字列）で分ける:

| 先頭の文字列 | 中身 | 現行の削除 | 前の版の削除 |
|---|---|---|---|
| `audit-raw/` | 監査ログの原本（保持 90 日） | 91 日 | 91 日 |
| `audit-access/` | 閲覧と管理操作の記録（保持 90 日） | 91 日 | 91 日 |
| `audit-derived/meta/`、`audit-derived/meta_day/` | 整形したデータ | 91 日 | 1 日 |
| `audit-derived/usage_daily/` | 日次の利用量の集計 | 400 日 | 1 日 |
| `audit-derived/billing/settlement/` | 月次の精算 | 400 日 | 400 日 |
| `audit-derived/billing/contracts/`、`fx_rates/` | 請求の単位と換算レート | 消さない | 消さない |
| `audit-derived/synth/` | 性能試験の合成データ | 7 日 | 1 日 |
| `pg-backup/` | PostgreSQL のバックアップと WAL | 消さない | 1 日 |
| `images/` | コンテナイメージの置き場 | 消さない | 1 日 |
| `cluster/` | k3s のトークンと版 | 消さない | 30 日 |

## 6. IAM ユーザー 2 つとアクセスキー

| IAM ユーザー | 使う所 | 許すこと | 許さないこと |
|---|---|---|---|
| litellm-writer | LiteLLM（監査ログを書く） | `audit-raw/raw/` に置く、保持を付ける | 読む、消す、上書き、ほかのディレクトリ |
| lab-app | 整形の定期処理、検索アプリ、DB のバックアップ、イメージ置き場、k3s の設定、構築ツールの操作者 | 一覧とバケットの設定の読み取り、`audit-raw/` `audit-access/` `audit-derived/` を読む、`audit-access/`（保持付き）と `audit-derived/` に置く、`audit-derived/` を消す、`pg-backup/` `images/` `cluster/` の全操作 | 原本と閲覧記録を消す・保持を外す |

両方に明示的な拒否: `audit-raw/` と `audit-access/` の版の削除、保持の回避、リーガルホールドの変更。バケットの削除、オブジェクトロック・ライフサイクル・バージョニングの変更。
ポリシーの JSON は `./lab provider-requirements` が出す。

受け取る物: 2 ユーザーそれぞれのアクセスキー ID とシークレット（発行時に 1 回だけ表示される）。受け渡しはパスワード管理ツールの共有など、平文のメールやチャット以外で。
鍵の作り直し（ローテーション）は、同じユーザーに 2 本目を発行 → こちらが `.env` を書き換えて `./lab secrets` と `./lab litellm` を流す → 古い方を無効化、の順。

## 7. DNS（Cloudflare）

| 項目 | 依頼内容 |
|---|---|
| ゾーン | 利用者が呼ぶホスト名（例 `litellm-lab.<ドメイン>`）のゾーン |
| API トークン | そのゾーンに限って `Zone / DNS / Edit` と `Zone / Zone / Read`。構築ツールが A レコード（ホスト名 → ロードバランサの内部 IP、Mac 用の別名 → 127.0.0.1）と Let's Encrypt の DNS-01 の TXT を書く |

社内 DNS を使う場合は、ホスト名 → ロードバランサの内部 IP の A レコードを登録してもらい、証明書の取り方を別に決める（未設計）。

## 8. Zabbix（既存サーバーを使う場合）

| 項目 | 依頼内容 |
|---|---|
| 宛先 | Zabbix サーバー（または proxy）のホスト名と 10051 |
| API | URL（`https://<サーバー>/api_jsonrpc.php`）と API トークン。権限は「管理者」の役割（ホストグループ・ホスト・プロキシ・自動登録のアクション・アイテム・トリガーの作成と更新、テンプレートの参照） |
| 自動登録 | VM 3 台と踏み台の agent 2 が能動で送る。HostMetadata は `litellm-lab-node` と `litellm-lab-bastion` |

## 9. 構築ツールの操作者の権限（クラウドの API、読み取り）

| 操作 | 権限 |
|---|---|
| VM の情報の取得 | `ec2:DescribeInstances` |
| ロードバランサの情報と健全性 | `elasticloadbalancing:DescribeLoadBalancers` / `DescribeTargetGroups` / `DescribeTargetHealth` |
| 障害の試験（本番で行う場合だけ） | `ec2:StopInstances` / `StartInstances` |
| S3 | lab-app の鍵を使う（操作者用のユーザーは増やさない） |

## 10. 受け取る物と `.env` の項目

| 受け取る物 | `.env` の項目 |
|---|---|
| VM 3 台のインスタンス ID | `LAB_VM_NODES` |
| 足す台のインスタンス ID（足すとき） | `LAB_VM_WORKER_NODES` |
| SSH の秘密鍵と利用者名 | `LAB_VM_SSH_KEY`、`LAB_VM_SSH_USER` |
| 踏み台の宛先、利用者名、プライベート IP | `LAB_VM_BASTION_HOST`、`LAB_VM_BASTION_SSH_USER`、`LAB_VM_BASTION_CIDR` |
| ロードバランサの ARN | `LAB_VM_ENTRY_IP_ID` |
| VPC の CIDR | `LAB_VM_CIDR` |
| バケット名 | `LAB_VM_BUCKET` |
| 2 ユーザーのアクセスキー | `LAB_VM_S3_LITELLM_WRITER_KEY/_SECRET`、`LAB_VM_S3_LAB_APP_KEY/_SECRET` |
| Zscaler の URL と CA | `LAB_EGRESS_PROXY`、`LAB_EGRESS_CA_FILE`（Mac 側は `LAB_EGRESS_PROXY_MAC`） |
| Cloudflare のトークン | `LAB_CLOUDFLARE_API_TOKEN` |
| Zabbix の宛先と API | `LAB_ZABBIX_SERVER`、`LAB_ZABBIX_API_URL`、`LAB_ZABBIX_API_TOKEN` |

## 11. 未決・未確認

- 本番の DNS を Cloudflare にするか社内 DNS にするか（証明書の取り方が変わる）
- AWS での配備は未確認（GCP で確認済み。内部 NLB、ロール無しの EC2、IAM ユーザーのポリシーは実機で未確認）

## GCP の場合

| AWS | GCP |
|---|---|
| EC2、インスタンスロール無し | Compute Engine、サービスアカウント無し。CPU 4 個・メモリ 32 GB は例 c4a-highmem-4 |
| 足す台をターゲットグループに登録・解除 | 足す台をそのゾーンの非マネージドインスタンスグループに入れる・外す（そのゾーンのグループが無ければ作ってバックエンドサービスに足す） |
| 内部 NLB | 内部パススルー ネットワーク ロードバランサ（静的内部 IP、TCP のヘルスチェック 5 秒 × 2、ゾーンごとのインスタンスグループ、転送ルール）。識別子は転送ルールの名前。ヘルスチェックの送信元 35.191.0.0/16 と 130.211.0.0/22 を通す |
| S3（Object Lock） | Cloud Storage（オブジェクト保持ロックを作成時に有効、バージョニング、公開アクセスの防止、**均一なバケットレベルのアクセス**） |
| IAM ユーザー 2 つ | サービスアカウント 2 つと HMAC キー。lab-app はイメージ置き場（GCS ドライバ）用に JSON キーも（`LAB_VM_GCS_REGISTRY_KEY_JSON`）。権限はバケットの条件付き IAM バインディング（接頭辞の条件）とカスタムロール 2 つ（追記 ＋ 保持、一覧） |
| 操作者の読み取り | `compute.instances.get`、`compute.forwardingRules.get`、`compute.regionBackendServices.get` / `getHealth`（試験なら `compute.instances.stop` / `start`） |
