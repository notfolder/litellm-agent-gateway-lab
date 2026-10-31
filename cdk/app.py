#!/usr/bin/env python3
"""AWS 環境（LAB_TARGET=aws）の基盤を CloudFormation のスタックとして作る CDK アプリ
（変更仕様書 .history/2026-10-24-AWS構成/、変更設計書 3 章）。

./lab cluster が .build/aws-spec.json（labtool/aws.py の spec()）を書き、-c spec=<パス> で渡して配備する。
バケット、ライフサイクル、権限の表は labtool/render.py の定義をそのまま受け取る（1 つの定義から両方に渡す）。

スタック（接頭辞 <prefix>-）:
- cert:    ACM の証明書（DNS 検証。手持ちの DNS に CNAME を足すまで待つ）
- network: VPC（3 AZ、public / private）、NAT、VPC エンドポイント（S3、Bedrock、Secrets Manager）
- data:    S3 × 3（Object Lock）、Aurora PostgreSQL（Serverless v2）、ElastiCache for Valkey、EFS、ECR、
           Secrets Manager、Pod Identity 用の IAM ロール。計算資源を消しても残す
- compute: EKS Auto Mode、アクセスエントリ、Pod Identity の関連付け、EFS CSI のアドオン
"""

import json
import os
import sys

import aws_cdk as cdk

from lab_stacks import CertStack, ComputeStack, DataStack, NetworkStack

app = cdk.App()
spec_path = app.node.try_get_context("spec")
if not spec_path:
    sys.exit("使い方: cdk <コマンド> -c spec=<.build/aws-spec.json>（./lab cluster が書く）")
with open(spec_path, encoding="utf-8") as f:
    spec = json.load(f)

env = cdk.Environment(account=spec["account"], region=spec["region"])
prefix = spec["prefix"]
tags = {"Project": "litellm-agent-gateway-lab", "LabPrefix": prefix}

cert = CertStack(app, f"{prefix}-cert", spec=spec, env=env, tags=tags)
network = NetworkStack(app, f"{prefix}-network", spec=spec, env=env, tags=tags)
data = DataStack(app, f"{prefix}-data", spec=spec, vpc=network.vpc, env=env, tags=tags)
compute = ComputeStack(app, f"{prefix}-compute", spec=spec, vpc=network.vpc, data=data, env=env, tags=tags)
compute.add_stack_dependency(data)

app.synth()
