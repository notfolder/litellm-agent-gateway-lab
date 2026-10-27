# 構成図

検証環境は同じコード（審査ガードレール、監査ログ、マニフェスト、Helm の値）を Mac の k3d と AWS の EKS の両方で動かす。
`.env` の `LAB_TARGET`（`local` か `aws`）で、構築ツール `./lab` が配備先を切り替える。
細かい設計は [design.md](design.md)（Mac）と `.history/2026-10-24-AWS構成/変更設計書.md`（AWS）にある。
この文書は全体の形を図で示すだけで、値や手順はそちらを正とする。

## 1. 論理構成（両環境で同じ）

利用者のエージェントは LiteLLM のゲートウェイだけを向く。ゲートウェイがモデルの呼び出し、ツールコールの審査、監査ログの記録を一手に引き受ける。

```mermaid
flowchart LR
    subgraph CLIENTS["利用者"]
        CC["Claude Code"]
        CX["Codex"]
        BR["ブラウザ<br/>（管理画面・検索アプリ）"]
    end
    subgraph GW["ゲートウェイ（Kubernetes）"]
        IN["入口<br/>Mac: Traefik / AWS: ALB<br/>パススルーの遮断、MCP の GET の拒否"]
        LL["LiteLLM × 2〜4<br/>審査ガードレール<br/>監査ログのコールバック"]
        WS["google_workspace_mcp<br/>（Gmail の MCP）"]
        AV["検索アプリ × 2<br/>/lab-audit/"]
        CJ["整形の定期処理<br/>（毎時）"]
    end
    subgraph DATA["データストア"]
        PG[("PostgreSQL<br/>Mac: CloudNativePG / AWS: Aurora")]
        VK[("Valkey<br/>Mac: StatefulSet + Sentinel / AWS: ElastiCache")]
        S3[("オブジェクトストレージ<br/>Mac: RustFS / AWS: S3<br/>audit-raw / audit-access / audit-derived")]
    end
    subgraph LLM["モデルと審査官"]
        MODEL["Claude<br/>Mac: Anthropic API（トークン転送）<br/>AWS: Bedrock"]
        JUDGE["審査官 2 つ<br/>review-judge / isolated-judge<br/>Mac: ChatGPT / AWS: Bedrock Haiku"]
        OAI["ChatGPT（Codex のモデル）"]
    end
    CC & CX & BR --> IN
    IN --> LL
    IN --> AV
    LL --> PG & VK
    LL -->|"監査ログ（書けなければ退避して再送）"| S3
    LL --> MODEL & JUDGE & OAI
    LL -->|"利用者の Google トークン"| WS --> GAPI["Google API"]
    CJ -->|"raw → derived（Parquet）"| S3
    AV --> S3
```

## 2. Mac（k3d）の配置

OrbStack の中の k3d クラスタ 1 つ（server 1 台、agent 2 台）。入口と運用部品は server に固定し、データを持つ部品は 3 台に散らす。

```mermaid
flowchart TB
    subgraph MAC["Mac mini（OrbStack、メモリ上限 12 GB）"]
        CLI["Claude Code / Codex / ブラウザ<br/>→ http://localhost:8080"]
        REG["イメージの置き場 × 2<br/>（自作イメージ、docker.io のキャッシュ）"]
        subgraph K3D["k3d クラスタ litellm（k3s v1.35）"]
            subgraph SRV["server-0（制御部分、SQLite）"]
                TR["Traefik（入口）"]
                RF[("RustFS × 1<br/>監査ログ・バックアップ")]
                OPS["cert-manager<br/>CloudNativePG のオペレーター<br/>Barman Cloud Plugin"]
            end
            subgraph AG["agent-0 / agent-1（＋ server にも散る）"]
                LL["LiteLLM × 2〜4（PDB maxUnavailable 1）"]
                PG[("PostgreSQL × 3 ＋ Pooler × 2")]
                VK[("Valkey × 3 ＋ Sentinel × 3")]
                AV["検索アプリ × 2、整形の CronJob"]
                WS["google_workspace_mcp、偽 LLM"]
            end
        end
    end
    CLI --> TR --> LL
    TR --> AV
    LL --> PG & VK & RF
    PG -->|"WAL・バックアップ"| RF
    REG -.->|"イメージ"| K3D
    LL --> EXT["Anthropic API / ChatGPT / Google API"]
```

- データは PVC（local-path）に置く。`./lab down` でクラスタごと消える（`.env`、ChatGPT のトークン、イメージの置き場は残る）
- server を作り直すと入口と置き場が止まるので、Kubernetes の版上げは作り直し（[maintenance.md](maintenance.md) 3.3）

## 3. AWS の配置

CloudFormation の 4 スタック（cert / network / data / compute）を CDK で作り、クラスタの中身は Mac と同じマニフェストと Helm で入れる。
DNS は Cloudflare（Route 53 は使わない）。

```mermaid
flowchart TB
    subgraph OPS["Mac（./lab を流す）"]
        LAB["labtool（LAB_TARGET=aws）<br/>CDK deploy / docker push / kubectl / helm"]
        CLI["Claude Code / Codex / ブラウザ"]
    end
    CF["Cloudflare DNS<br/>CNAME（DNS のみ）"]
    subgraph AWS["AWS 東京（アカウント内、SSO で操作）"]
        ACM["ACM 証明書<br/>（cert スタック）"]
        ECR["ECR<br/>litellm-lab / audit-lab"]
        SM["Secrets Manager<br/>app / aurora / valkey-auth"]
        BED["Bedrock<br/>Claude Sonnet 4.5 / Haiku 4.5（jp. プロファイル）"]
        subgraph VPC["VPC 10.60.0.0/16（3 AZ）— network スタック"]
            subgraph PUB["public サブネット × 3"]
                ALB["ALB（Ingress が作る）<br/>443、パススルーの遮断"]
                NAT["NAT ゲートウェイ × 1"]
            end
            subgraph PRIV["private サブネット × 3"]
                subgraph EKS["EKS Auto Mode（compute スタック）"]
                    NP["NodePool lab-arm64<br/>（Graviton、on-demand）"]
                    LL["LiteLLM × 2〜4<br/>SA litellm（Pod Identity）"]
                    AV["検索アプリ × 2、整形の CronJob<br/>SA audit-viewer / audit-compactor"]
                    ESO["External Secrets Operator<br/>SA external-secrets"]
                    WS["google_workspace_mcp"]
                end
                RDS[("Aurora PostgreSQL 17<br/>Serverless v2、writer + reader")]
                EC[("ElastiCache for Valkey 9<br/>primary 1 + replica 2、Multi-AZ")]
                EFS[("EFS<br/>chatgpt/ のトークン共有")]
                VPCE["VPC エンドポイント<br/>S3（ゲートウェイ）<br/>Bedrock Runtime / Secrets Manager（インターフェース）"]
            end
        end
        S3[("S3 × 3（Object Lock）<br/>audit-raw / audit-access / audit-derived")]
    end
    CLI -->|"https://ホスト名"| CF --> ALB
    ALB --> LL & AV
    LL --> RDS & EC & EFS
    LL -->|"Pod Identity"| VPCE --> S3 & BED & SM
    AV -->|"Pod Identity"| S3
    ESO -->|"Pod Identity"| SM
    ESO -->|"Secret"| LL
    LAB -->|"cdk deploy"| AWS
    LAB -->|"docker push"| ECR -.->|"イメージ"| EKS
    LAB -->|"kubectl / helm"| EKS
    LAB -->|"秘密情報を生成"| SM
    ALB -.->|"証明書"| ACM
    EKS -->|"外向き（ChatGPT、Google API、ECR）"| NAT
```

- 状態を持つもの（Aurora、ElastiCache、S3、EFS、秘密情報）は data スタック。`./lab down` は compute → data → network の順に消し、スタックを消しても残るもの（Object Lock の S3、秘密情報、スナップショット、ECR、EFS、ロググループ）も消す。残るのは無料の証明書と CDK の bootstrap だけ
- Kubernetes の版上げは、制御部分を AWS が入れ替え、Auto Mode が PDB を守ってノードを 1 台ずつ入れ替えるので無停止でできる（[maintenance.md](maintenance.md) 3.3）

## 4. 部品の対応表

| 役割 | Mac（k3d） | AWS（EKS） | コードの置き場 |
|---|---|---|---|
| Kubernetes | k3d（k3s v1.35、server 1 + agent 2） | EKS Auto Mode 1.35（NodePool lab-arm64） | `k8s/k3d-config.yaml` / `cdk/` |
| 入口 | Traefik（`k8s/ingress.yaml`） | ALB（Ingress。`render.alb_ingress_manifest`） | 遮断する経路の一覧は両方で同じ（単体テストが照合） |
| TLS | なし（localhost） | ACM の証明書、Cloudflare の CNAME | `cdk/`（cert スタック） |
| PostgreSQL | CloudNativePG × 3 ＋ Pooler | Aurora Serverless v2（writer + reader） | `k8s/postgres-cnpg.yaml` / `cdk/` |
| Redis 互換 | Valkey × 3 ＋ Sentinel × 3 | ElastiCache for Valkey（primary 1 + replica 2） | `k8s/valkey.yaml` / `cdk/` |
| オブジェクトストレージ | RustFS × 1（静的キー） | S3 × 3（Pod Identity、Object Lock） | `render.s3_buckets()`、`render.s3_policies()` を両方で使う |
| 秘密情報 | `.env` → Secret | Secrets Manager → External Secrets Operator → Secret | `./lab secrets` |
| コンテナイメージ | k3d のレジストリ | ECR | `./lab images` |
| Claude のモデル | Anthropic API（トークン転送） | Bedrock（jp. 推論プロファイル） | `render.litellm_config()` |
| 審査官 | ChatGPT（`chatgpt/`） | Bedrock Haiku 4.5 | 同上 |
| Codex のモデル | ChatGPT（`chatgpt/`） | 同じ（トークンは EFS で共有） | 同上 |
| chatgpt/ のトークン | hostPath | EFS の PVC | `./lab chatgpt-login` |
| LLM の審査（Jev） | `LAB_USE_JEV` | 使わない | `litellm/guardrails/` |
