# cdk/ — AWS 環境の基盤（LAB_TARGET=aws）

`./lab cluster`（labtool/aws.py）が `.build/aws-spec.json` を書き、`npx aws-cdk@2 deploy ... -c spec=<パス>` で
4 つのスタックを配備する。手で流すときも同じ:

```sh
cd cdk
npx --yes aws-cdk@2 synth -c spec=../.build/aws-spec.json      # テンプレートの生成だけ（資格情報が要る）
npx --yes aws-cdk@2 diff  -c spec=../.build/aws-spec.json
```

| スタック | 中身 | 消してよいか |
|---|---|---|
| `<prefix>-cert` | ACM の証明書（DNS 検証） | 残す |
| `<prefix>-network` | VPC、NAT、VPC エンドポイント | 残す |
| `<prefix>-data` | S3（Object Lock）、Aurora、ElastiCache、EFS、ECR、Secrets Manager、Pod Identity のロール | 残す（監査ログは消せない） |
| `<prefix>-compute` | EKS Auto Mode、アクセスエントリ、Pod Identity の関連付け、EFS CSI | `./lab down` で消して作り直せる |

Python の依存は uv が `.venv` に用意する（`cdk.json` の app が `uv run`）。
