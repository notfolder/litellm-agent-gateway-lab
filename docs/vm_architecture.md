# 構成図（VM 3 台とオブジェクトストレージだけの環境、`LAB_TARGET=aws-ec2` / `gcp-vm`）

マネージドサービスを使わず、クラウドの部品を計算資源とオブジェクトストレージに限って、Mac の k3d と同じ中身を動かす環境。
AWS は EC2 3 台と S3、GCP は Compute Engine 3 台と Cloud Storage。クラウドの違いは `labtool/clouds/` に閉じ、VM の中と Kubernetes の中は 1 つのコード。
Mac と EKS の構成図は [aws_architecture.md](aws_architecture.md) にある。
細かい設計は `.history/2026-10-30-VM環境の共通化とGCP追加/変更設計書.md`（2 クラウドの層）、`.history/2026-10-30-既存資源からの構築と出口プロキシ/変更設計書.md`
（既存資源からの構築、1 バケット、出口プロキシ、VM 上のビルド）、`.history/2026-10-26-k3s版上げ/変更設計書.md`（Kubernetes の版上げ）にある。
この文書は全体の形を図で示すだけで、値や手順はそちらと `docs/design.md` 21 章を正とする。

## 1. 論理構成

利用者のエージェントは LiteLLM のゲートウェイだけを向く。審査ガードレール、監査ログ、検索アプリの層は環境を知らず、Mac と同じコードが動く。
Mac と違うのは、オブジェクトストレージが RustFS からクラウドのバケット 1 つに替わること、審査官が Anthropic の API キーの Claude Haiku 4.5 になること、
Codex のモデル（ChatGPT）を置かないことだけ。Claude Code のモデルは Mac と同じ Claude Max のトークンの転送。

```mermaid
flowchart LR
    subgraph CLIENTS["利用者"]
        CC["Claude Code"]
        BR["ブラウザ<br/>（管理画面・検索アプリ）"]
    end
    subgraph GW["ゲートウェイ（k3s、VM 3 台）"]
        IN["入口<br/>浮動 IP → Traefik<br/>HTTPS（Let's Encrypt）、パススルーの遮断、MCP の GET の拒否"]
        LL["LiteLLM × 2〜4<br/>審査ガードレール<br/>監査ログのコールバック"]
        WS["google_workspace_mcp<br/>（Gmail の MCP）"]
        AV["検索アプリ × 2<br/>/lab-audit/"]
        CJ["整形の定期処理<br/>（毎時）"]
    end
    subgraph DATA["データストア"]
        PG[("PostgreSQL<br/>CloudNativePG × 3")]
        VK[("Valkey × 3<br/>＋ Sentinel × 3")]
        S3[("バケット 1 つ（S3 / Cloud Storage）<br/>audit-raw/ audit-access/ audit-derived/<br/>pg-backup/ images/ cluster/")]
    end
    subgraph LLM["モデルと審査官"]
        MODEL["Anthropic API<br/>（Claude Max のトークンを転送）"]
        JUDGE["審査官 2 つ<br/>review-judge / isolated-judge<br/>Claude Haiku 4.5（API キー）"]
    end
    PX["出口プロキシ（Zscaler）<br/>独自 CA で TLS を終端し直す"]
    CC & BR --> IN
    IN --> LL
    IN --> AV
    LL --> PG & VK
    LL -->|"監査ログ（原本には保持を付ける）"| PX
    LL --> PX
    PX --> MODEL & JUDGE & S3 & GAPI["Google API"]
    LL -->|"利用者の Google トークン"| WS --> PX
    PG -->|"WAL・バックアップ"| PX
    CJ -->|"raw → derived（Parquet）"| PX
    AV --> PX
```

出口プロキシは本番の前提（Zscaler の中）。無い環境では各部品が直接外へ出る（`.env` の `LAB_EGRESS_PROXY` が空）。

## 2. クラウドの配置

| 部品 | AWS（`aws-ec2`） | GCP（`gcp-vm`） |
|---|---|---|
| 計算資源 | EC2 × 3（t4g.large、Ubuntu 24.04 arm64、別 AZ の公開サブネット、プライベート IP 2 つ） | Compute Engine × 3（C4A、Ubuntu 24.04 arm64、別ゾーン、Hyperdisk） |
| ネットワーク | VPC 10.60.0.0/16、サブネット /24 × 3、IGW、S3 のゲートウェイエンドポイント、セキュリティグループ | VPC（カスタム）、サブネット /24 × 1、ファイアウォール（web / internal / ssh / proxy-in / egress） |
| 浮動 IP | Elastic IP（主系の 2 つ目のプライベート IP に付く） | 静的な外部 IP（主系のアクセス構成に付く。降りた台には一時的な IP） |
| オブジェクトストレージ | S3 × 1（Object Lock 有効、既定の保持なし、バージョニング） | Cloud Storage × 1（オブジェクト保持ロック、バージョニング、均一なバケットレベルのアクセス） |
| 静的キー | IAM ユーザー × 5（ディレクトリ単位の権限） | サービスアカウント × 6（操作者を含む）。HMAC キー（registry は GCS ドライバ用の JSON キー）、条件付きバインディングとカスタムロール |
| VM の権限 | インスタンスロール（Elastic IP の付け替えだけ） | サービスアカウント ＋ カスタムロール（外部 IP の付け替えだけ） |
| 中継（検証だけ） | t3.small（amd64）、専用のセキュリティグループ | e2-small（amd64）、タグ `<prefix>-proxy`、egress の deny で 3 台を塞ぐ |

```mermaid
flowchart TB
    subgraph OPS["操作者の Mac（./lab を流す。本番は Zscaler の中）"]
        LAB["labtool（LAB_TARGET=gcp-vm / aws-ec2、LAB_INFRA=managed / provided）<br/>クラウドの API（provision）／SSH（初期化、ビルド、6443 の転送）／kubectl・helm"]
        CLI["Claude Code / ブラウザ"]
    end
    CF["Cloudflare DNS<br/>A レコード（./lab が作る）<br/>DNS-01 の TXT（cert-manager）"]
    ANT["Anthropic API"]
    subgraph CLOUD["クラウド（東京）"]
        EIP["浮動 IP<br/>（主系の台に付く）"]
        subgraph VPC["VPC 10.60.0.0/16"]
            PX["中継の VM（.20、amd64）<br/>mitmproxy、独自 CA、3128<br/>（検証だけ。本番は Zscaler）"]
            subgraph NODES["VM × 3（Ubuntu 24.04、k3s server × 3、組み込み etcd）"]
                KA["keepalived（OS 上）<br/>k3s と :80 を監視、主系が浮動 IP を取る"]
                DK["Docker（自作イメージのビルド）"]
                subgraph K3S["k3s（PVC は local-path）"]
                    TR["Traefik × 2<br/>80/443、80 → 443"]
                    CM["cert-manager × 2<br/>Let's Encrypt DNS-01"]
                    REG["registry × 2（保存先は images/）<br/>NodePort 30500、各台が自分の IP から引く"]
                    LL["LiteLLM × 2〜4"]
                    PG[("PostgreSQL × 3 ＋ Pooler × 2<br/>オペレーター × 2")]
                    VK[("Valkey × 3 ＋ Sentinel × 3")]
                    AV["検索アプリ × 2、整形の CronJob<br/>google_workspace_mcp"]
                end
            end
        end
        S3[("バケット × 1<br/>audit-raw/（保持 90 日）audit-access/（保持 400 日）<br/>audit-derived/ pg-backup/ images/ cluster/")]
        KEYS["静的キー × 5〜6（.env の LAB_VM_S3_*）"]
    end
    CLI -->|"https://ホスト名"| CF --> EIP --> TR
    TR --> LL & AV
    LL --> PG & VK
    LL & PG & AV & REG & CM -->|"中継経由（CA を信頼）"| PX
    PX --> S3 & ANT & CF
    KA -->|"浮動 IP の付け替え（クラウドの API。中継経由）"| EIP
    KEYS -.->|".env → Secret"| K3S
    LAB -->|"provision（managed だけ）"| CLOUD
    LAB -->|"ssh: 初期化・docker build・6443 の転送"| NODES
    LAB -->|"中継経由（LAB_EGRESS_PROXY_MAC）"| PX
```

- 1 台が落ちても処理が続く（etcd、PostgreSQL、Sentinel はいずれも 3 台の多数決）。入口の台が落ちると、keepalived の検知（約 3 秒）と浮動 IP の付け替えで別の台に移る（障害の試験 `--scenario entry-node`。GCP の付け替えは 80〜150 秒、AWS は数秒）
- AWS は各台がプライベート IP を 2 つ持ち、Elastic IP は 2 つ目に付く（移っても外向きの通信が途切れない）。GCP は主系の外部 IP が静的 IP に入れ替わるので、構築ツールは公開 IP を引き直す
- データは各台の PVC（local-path）に置き、PostgreSQL のバックアップと監査ログはバケットに出る

## 3. 基盤の持ち方（`LAB_INFRA`）

```mermaid
flowchart LR
    subgraph MANAGED["managed（検証）"]
        P["provision<br/>ネットワーク → 浮動 IP → バケット → 静的キー → VM の権限 → VM 3 台 →（中継の VM）"] --> E1[".env に識別子と静的キーを書く"]
    end
    subgraph PROVIDED["provided（本番。既存の VM 3 台とバケット 1 つ）"]
        H["用意する側が VM・バケット・浮動 IP・権限・静的キーを作る"] --> E2[".env に識別子と静的キーを書く（人）"]
    end
    E1 & E2 --> S["setup（共通）<br/>SSH で VM を初期化（k3s のトークンと版、CA、中継）→ ポート転送 → Kubernetes の中"]
```

- provided では、構築ツールはネットワーク・ファイアウォール・浮動 IP・VM の権限・バケットの設定を変えない（合わなければ止まるか警告する）。用意する側への前提は `docs/design.md` 21.9 節
- `./lab down` は managed なら全部消し、provided なら k3s を外して保持の無いディレクトリを空にする。`./lab down --all` で provided でも VM・浮動 IP・バケットまで消す

## 4. 1 台の中（SSH での初期化）

各台は構築ツールが SSH で流す同じスクリプト（`vm/node-init.sh` ＋ クラウドごとの `vm/hooks-*.sh`）で初期化し、1 番の台だけがクラスタを作る。
参加先は配備済みの台から探すので、1 台を作り直しても別のクラスタにならない。2 回目以降は中継と CA の設定だけ直して抜ける。

```mermaid
flowchart LR
    subgraph NODE["VM 1 台（Ubuntu 24.04）"]
        INIT["node-init.sh（SSH、root）<br/>CA を信頼店に → 中継を apt / Docker / k3s / シェルに → keepalived・Docker を入れる<br/>→ containerd のミラー設定 → /etc/rancher/k3s/config.yaml → k3s のインストール（トークンは引数）<br/>→ keepalived と hooks → 起動時に外向き IP を戻す systemd"]
        K3S["k3s server<br/>（組み込み etcd、containerd、flannel）"]
        KA["keepalived<br/>ユニキャスト VRRP、3 台とも BACKUP、nopreempt"]
        DK["Docker（ビルド用）<br/>127.0.0.1:30500 の registry に push"]
    end
    LAB["構築ツール<br/>（トークンと版はバケットの cluster/ から読んで SSH で渡す）"]
    OTHER["他の 2 台"]
    EIP["浮動 IP"]
    LAB -->|ssh| INIT --> K3S & KA & DK
    K3S <-->|"etcd の多数決、API"| OTHER
    KA <-->|"VRRP"| OTHER
    KA -->|"主系になったら付け替え（hooks）"| EIP
```

## 5. バケット 1 つのディレクトリ構成と保持

| ディレクトリ | 中身 | 保持 | 書く利用者 |
|---|---|---|---|
| `audit-raw/` | 監査ログの原本（`raw/meta`、`raw/body`） | オブジェクト単位、90 日（書くときに付ける） | litellm-writer（追記と保持だけ。読めない・消せない） |
| `audit-access/` | 閲覧と管理操作の記録 | オブジェクト単位、400 日 | audit-viewer |
| `audit-derived/` | 整形済み（Parquet）、集計、精算、請求の単位 | 無し（ライフサイクルで期限） | audit-compactor、audit-viewer（請求の単位） |
| `pg-backup/` | PostgreSQL の WAL とバックアップ | 無し | pg-backup（Barman） |
| `images/` | クラスタの中の registry の保存先 | 無し | registry |
| `cluster/` | k3s のトークンと版 | 無し | 操作者 |

バケット全体の既定の保持は使わない（GCS は上書きも拒むので registry や整形が止まる）。保持の方式は S3 が Object Lock（GOVERNANCE / COMPLIANCE）、GCS がオブジェクト保持ロック（Unlocked / Locked）。
Mac の RustFS は従来の 4 バケットとバケット既定の保持のまま。

## 6. 出口プロキシ（Zscaler 相当）の中での構築

| 層 | 外へ出る通信 | 配り方 |
|---|---|---|
| VM | apt、k3s のインストール、containerd のイメージ取得、Docker のビルド、クラウドの API（浮動 IP） | CA を OS の信頼店に。`/etc/environment`、apt、Docker、`k3s.service.env` に中継。メタデータサーバーは中継を通さない |
| Pod | LiteLLM → Anthropic・Google、MCP → Google、cert-manager → Let's Encrypt・Cloudflare、Barman・registry・監査アプリ → バケット | ConfigMap の CA をマウントし、`HTTPS_PROXY` / `NO_PROXY` / `SSL_CERT_FILE` 系の環境変数（`render.add_egress`）。cert-manager は Helm の値、Barman は `endpointCA` と sidecar の env |
| 操作者の Mac | `./lab`（boto3、Google のクライアント、Cloudflare）、helm | `LAB_EGRESS_PROXY_MAC` で環境変数と helm の `--ca-file`。ssh と kubectl（SSH の転送）は中継を通らない |

検証では 4 台目の VM に mitmproxy（明示プロキシ、独自 CA）を置き、3 台の直接の外向き通信を塞いで全段階と全試験を通す（`LAB_EGRESS_PROXY_VM=true`）。
中継の記録は `./lab proxy-log` で宛先ホストごとに数えられる。

## 7. Kubernetes（k3s）の版上げ（`./lab upgrade-k8s <版>`）

版はバケットの `cluster/k3s/version` に置き、`./lab up` では変えない。上げるときは 1 台ずつ、浮動 IP を持つ台を最後にする。マイナー版は 1 つずつで、飛ばす・下げる指示は止める。
途中で止まっても再実行で続きから進む。`--measure` で上げている間の要求の失敗数と幅を測る（経緯は `.history/2026-10-26-k3s版上げ/`）。

## 8. Mac の k3d との対応

| 役割 | Mac（k3d） | VM 3 台（k3s） | コードの置き場 |
|---|---|---|---|
| Kubernetes | k3d（server 1 + agent 2） | k3s（server 3、組み込み etcd）。版上げは `./lab upgrade-k8s` | `k8s/k3d-config.yaml` / `vm/node-init.sh` |
| 基盤 | OrbStack | managed: `labtool/clouds/*.provision`。provided: 用意する側 ＋ `.env` | `labtool/clouds/`、`labtool/vm.py` |
| 入口 | Traefik × 1 | Traefik × 2 ＋ keepalived ＋ 浮動 IP | 遮断する経路の一覧は同じ |
| TLS | なし（localhost） | cert-manager ＋ Let's Encrypt（DNS-01）、Cloudflare の A レコード（`./lab` が作る） | `render.vm_issuer_manifest` |
| PostgreSQL / Valkey | CloudNativePG × 3、Valkey × 3 | 同じ（バックアップ先は `pg-backup/`） | `k8s/` |
| オブジェクトストレージ | RustFS × 1、4 バケット、既定の保持 | バケット × 1、ディレクトリ、オブジェクト単位の保持 | `render.store_location`、`objectstore.vm_lifecycle_rules`、`clouds.s3_user_policies` / `gcs_bindings` |
| 秘密情報 | `.env` → Secret | `.env`（`LAB_VM_S3_*`）→ Secret | `./lab secrets` |
| コンテナイメージ | Mac の Docker → k3d のレジストリ | VM の Docker でビルド → クラスタの中の registry × 2（保存先は `images/`。AWS は S3 ドライバ、GCP は GCS ドライバ） | `vm.build_and_push_on_node` |
| 外向きの通信 | 直接 | 中継（Zscaler）経由、CA を信頼 | `render.add_egress`、`vm/node-init.sh` |
| 審査官 / Claude のモデル | ChatGPT / Anthropic API（トークン転送） | Anthropic API キーの Haiku 4.5 / 同じ | `render.litellm_config()` |
| 構築ツールの経路 | localhost | SSH の 6443 転送（`litellm-lab-ec2` / `litellm-lab-gcp` のコンテキスト） | `labtool/vm.py` |
| 障害の試験 | DB・Redis・LiteLLM の場面 | 同じ ＋ `entry-node` | `labtool/failover.py` |
