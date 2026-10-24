"""AWS 環境の 4 つのスタック（変更設計書 .history/2026-10-24-AWS構成/変更設計書.md 3 章）。

設計の要点:
- 状態を持つもの（S3、Aurora、ElastiCache、EFS、ECR、Secrets Manager、IAM ロール）は data に置き、
  計算資源（EKS）を消しても残す。監査ログのバケットは Object Lock で消せないので、消す対象から構造的に分ける
- S3 のバケット・ライフサイクル・権限の表は labtool/render.py の定義（spec["buckets"]、spec["policies"]）を
  そのまま使う。IAM の Resource の ARN は論理名に接頭辞を付けて作る
- EKS は Auto Mode。組み込みのノードプールは system だけ（default の NodeClass を用意させるため）。
  一般の Pod は labtool が入れる arm64 のノードプール（lab-arm64）に載る
- Pod の AWS への権限は EKS Pod Identity（IAM ロール ↔ ServiceAccount）。静的なアクセスキーは作らない
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import (
    aws_certificatemanager as acm,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_efs as efs,
    aws_eks as eks,
    aws_elasticache as elasticache,
    aws_iam as iam,
    aws_rds as rds,
    aws_s3 as s3,
    aws_secretsmanager as sm,
)
from constructs import Construct

POD_IDENTITY_TRUST = iam.ServicePrincipal("pods.eks.amazonaws.com")


def pod_identity_role(scope: Construct, id_: str, role_name: str) -> iam.Role:
    """EKS Pod Identity が引き受けられる IAM ロール（sts:AssumeRole と sts:TagSession を許す）。"""
    role = iam.Role(scope, id_, role_name=role_name, assumed_by=POD_IDENTITY_TRUST)
    # ServicePrincipal の既定は sts:AssumeRole だけなので、TagSession を足す
    role.assume_role_policy.add_statements(iam.PolicyStatement(
        actions=["sts:TagSession"], principals=[POD_IDENTITY_TRUST]))
    return role


class CertStack(cdk.Stack):
    """ACM の証明書（DNS 検証）。手持ちの DNS に検証用の CNAME を足すまで、CloudFormation は待つ。"""

    def __init__(self, scope: Construct, id_: str, *, spec: dict, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)
        cert = acm.Certificate(self, "Certificate", domain_name=spec["hostname"],
                               validation=acm.CertificateValidation.from_dns())
        cert.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
        self.certificate_arn = cert.certificate_arn
        cdk.CfnOutput(self, "CertificateArn", value=cert.certificate_arn)


class NetworkStack(cdk.Stack):
    """VPC（3 AZ）。public（ALB）、private（EKS のノードと Pod、Aurora、ElastiCache、EFS）。
    S3 はゲートウェイ型、Bedrock と Secrets Manager はインターフェース型の VPC エンドポイント。"""

    def __init__(self, scope: Construct, id_: str, *, spec: dict, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)
        self.vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.60.0.0/16"),
            availability_zones=spec["availability_zones"],
            nat_gateways=spec["nat_gateways"],
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=22),
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=20),
            ],
            gateway_endpoints={"S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)},
        )
        # EKS Auto Mode の ALB がサブネットを見つけるためのタグ（public: elb、private: internal-elb）
        for sn in self.vpc.public_subnets:
            cdk.Tags.of(sn).add("kubernetes.io/role/elb", "1")
        for sn in self.vpc.private_subnets:
            cdk.Tags.of(sn).add("kubernetes.io/role/internal-elb", "1")
        for name, service in (("Bedrock", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
                              ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER)):
            self.vpc.add_interface_endpoint(name, service=service,
                                            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))
        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)


class DataStack(cdk.Stack):
    """消えてはいけないもの: S3（監査ログ）、Aurora、ElastiCache、EFS、ECR、Secrets Manager、Pod Identity のロール。"""

    def __init__(self, scope: Construct, id_: str, *, spec: dict, vpc: ec2.Vpc, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)
        prefix = spec["prefix"]
        self.spec = spec
        self.buckets = self._buckets(spec)
        self.roles = self._pod_identity_roles(spec)
        self.aurora = self._aurora(spec, vpc)
        self.valkey_auth, self.valkey = self._valkey(spec, vpc)
        self.efs = self._efs(spec, vpc)
        self.ecr = {name: ecr.Repository(
            self, f"Ecr{name.title().replace('-', '')}", repository_name=f"{prefix}/{name}",
            image_scan_on_push=False, empty_on_delete=False, removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)]) for name in ("litellm-lab", "audit-lab")}
        # アプリの秘密情報 {prefix}/app は ./lab secrets（labtool/aws.py）が作って育てる。ここでは読む権限だけを配る
        app_secret_arn = self.format_arn(service="secretsmanager", resource="secret", resource_name=f"{prefix}/app-*")
        self.roles["external-secrets"].add_to_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[app_secret_arn, self.aurora.secret.secret_arn, self.valkey_auth.secret_arn]))
        cdk.CfnOutput(self, "EcrRegistry", value=f"{self.account}.dkr.ecr.{self.region}.amazonaws.com")
        cdk.CfnOutput(self, "BucketPrefix", value=spec["bucket_prefix"])
        cdk.CfnOutput(self, "AuroraClusterIdentifier", value=self.aurora.cluster_identifier)
        cdk.CfnOutput(self, "AuroraClusterResourceId", value=self.aurora.cluster_resource_identifier)
        cdk.CfnOutput(self, "AuroraWriterEndpoint", value=self.aurora.cluster_endpoint.hostname)
        cdk.CfnOutput(self, "AuroraSecretName", value=self.aurora.secret.secret_name)
        cdk.CfnOutput(self, "ValkeyReplicationGroupId", value=self.valkey.ref)
        cdk.CfnOutput(self, "ValkeyPrimaryEndpoint", value=self.valkey.attr_primary_end_point_address)
        cdk.CfnOutput(self, "ValkeyPort", value=self.valkey.attr_primary_end_point_port)
        cdk.CfnOutput(self, "ValkeyAuthSecretName", value=self.valkey_auth.secret_name)
        cdk.CfnOutput(self, "EfsFileSystemId", value=self.efs.file_system_id)
        for key, role in self.roles.items():
            cdk.CfnOutput(self, f"Role{key.title().replace('-', '')}Arn", value=role.role_arn)

    # ---- S3 ----
    def _buckets(self, spec: dict) -> dict[str, s3.Bucket]:
        out: dict[str, s3.Bucket] = {}
        for logical, b in spec["buckets"].items():
            lock = b["lock"]
            kwargs: dict = {
                "bucket_name": spec["bucket_prefix"] + logical,
                "versioned": b["versioning"],
                "block_public_access": s3.BlockPublicAccess.BLOCK_ALL,
                "encryption": s3.BucketEncryption.S3_MANAGED,
                "enforce_ssl": True,
                "removal_policy": cdk.RemovalPolicy.RETAIN,
                "lifecycle_rules": [self._lifecycle(rule) for rule in b["lifecycle"]],
            }
            if lock:
                # Object Lock はバケットの作成時にだけ有効にできる（後から足せない）
                retention = cdk.Duration.days(lock["Days"])
                kwargs["object_lock_enabled"] = True
                kwargs["object_lock_default_retention"] = (
                    s3.ObjectLockRetention.compliance(retention) if lock["Mode"] == "COMPLIANCE"
                    else s3.ObjectLockRetention.governance(retention))
            out[logical] = s3.Bucket(self, f"Bucket{logical.title().replace('-', '')}", **kwargs)
        return out

    @staticmethod
    def _lifecycle(rule: dict) -> s3.LifecycleRule:
        kwargs: dict = {"id": rule["ID"], "enabled": rule.get("Status", "Enabled") == "Enabled"}
        prefix = (rule.get("Filter") or {}).get("Prefix", "")
        if prefix:
            kwargs["prefix"] = prefix
        if "Expiration" in rule:
            kwargs["expiration"] = cdk.Duration.days(rule["Expiration"]["Days"])
        if "NoncurrentVersionExpiration" in rule:
            kwargs["noncurrent_version_expiration"] = cdk.Duration.days(
                rule["NoncurrentVersionExpiration"]["NoncurrentDays"])
        return s3.LifecycleRule(**kwargs)

    # ---- Pod Identity の IAM ロール ----
    def _pod_identity_roles(self, spec: dict) -> dict[str, iam.Role]:
        prefix = spec["prefix"]
        roles: dict[str, iam.Role] = {}
        for key in spec["pod_identities"]:
            roles[key] = pod_identity_role(self, f"Role{key.title().replace('-', '')}", f"{prefix}-{key}")
        # S3 の権限表（labtool/render.py の s3_policies と同じ）。論理名に接頭辞を付けて ARN にする
        for user, doc in spec["policies"].items():
            for stmt in doc["Statement"]:
                resources = [self._bucket_arn(spec, r) for r in stmt["Resource"]]
                roles[user].add_to_policy(iam.PolicyStatement(
                    effect=iam.Effect.ALLOW if stmt["Effect"] == "Allow" else iam.Effect.DENY,
                    actions=stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]],
                    resources=resources))
        # どの利用者にも、原本（audit-raw）と閲覧の記録（audit-access）の削除と、保持の回避は許さない（明示的な拒否）
        locked = [self._bucket_arn(spec, f"{b}/*") for b in spec["buckets"] if spec["buckets"][b]["lock"]]
        for user in spec["policies"]:
            roles[user].add_to_policy(iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
                         "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:PutBucketObjectLockConfiguration"],
                resources=locked))
        # LiteLLM の Pod は Bedrock も呼ぶ（Claude Code のモデルと 2 つの審査官。推論プロファイル経由）
        roles["litellm-writer"].add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["arn:aws:bedrock:*:*:inference-profile/*", "arn:aws:bedrock:*::foundation-model/*"]))
        roles["efs-csi"].add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEFSCSIDriverPolicy"))
        return roles

    def _bucket_arn(self, spec: dict, resource: str) -> str:
        """権限表の arn:aws:s3:::<論理名>[/...] を、接頭辞付きのバケット名の ARN にする。"""
        name = resource.removeprefix("arn:aws:s3:::")
        return "arn:aws:s3:::" + spec["bucket_prefix"] + name

    # ---- Aurora ----
    def _aurora(self, spec: dict, vpc: ec2.Vpc) -> rds.DatabaseCluster:
        sg = ec2.SecurityGroup(self, "AuroraSg", vpc=vpc, description="Aurora PostgreSQL (lab)")
        sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(5432), "from inside the VPC")
        cluster = rds.DatabaseCluster(
            self, "Aurora",
            cluster_identifier=f"{spec['prefix']}-pg",
            engine=rds.DatabaseClusterEngine.aurora_postgres(version=rds.AuroraPostgresEngineVersion.VER_17_6),
            # マスターユーザー litellm（RDS が Secrets Manager にパスワードを置く。ESO が Secret にする）
            credentials=rds.Credentials.from_generated_secret("litellm", secret_name=f"{spec['prefix']}/aurora"),
            default_database_name="litellm",
            writer=rds.ClusterInstance.serverless_v2("writer"),
            readers=[rds.ClusterInstance.serverless_v2("reader", scale_with_writer=True)],
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=spec["aurora_max_acu"],
            serverless_v2_auto_pause_duration=cdk.Duration.minutes(spec["aurora_pause_minutes"]),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[sg],
            storage_encrypted=True,
            backup=rds.BackupProps(retention=cdk.Duration.days(30)),   # 任意の時点への復元は 30 日（仕様書 5.1）
            deletion_protection=spec["scale"] != "lab",
            removal_policy=cdk.RemovalPolicy.SNAPSHOT,
        )
        return cluster

    # ---- ElastiCache for Valkey ----
    def _valkey(self, spec: dict, vpc: ec2.Vpc) -> tuple[sm.Secret, elasticache.CfnReplicationGroup]:
        prefix = spec["prefix"]
        auth = sm.Secret(self, "ValkeyAuth", secret_name=f"{prefix}/valkey-auth",
                         generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=48))
        subnets = elasticache.CfnSubnetGroup(
            self, "ValkeySubnets", description="Valkey (lab)",
            subnet_ids=[s.subnet_id for s in vpc.private_subnets], cache_subnet_group_name=f"{prefix}-valkey")
        sg = ec2.SecurityGroup(self, "ValkeySg", vpc=vpc, description="ElastiCache for Valkey (lab)")
        sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(6379), "from inside the VPC")
        # メモリが溢れても黙ってデータを消さない（仕様書 5.2）。min-replicas-to-write 1 は Mac の Valkey と同じ条件
        # （replica が 1 台も追従していなければ書き込みを拒む）。ElastiCache の durability（2 AZ に複製してから返す）は
        # Graviton の M/R/C 系の .large 以上とクラスターモード有効が必須で検証の規模に合わないので使わない（2026-10-24）
        params = elasticache.CfnParameterGroup(
            self, "ValkeyParams", cache_parameter_group_family="valkey9",
            description="noeviction, min-replicas-to-write 1 (lab)",
            properties={"maxmemory-policy": "noeviction", "min-replicas-to-write": "1", "min-replicas-max-lag": "10"})
        group = elasticache.CfnReplicationGroup(
            self, "Valkey",
            replication_group_id=f"{prefix}-valkey",
            replication_group_description="LiteLLM Redis-compatible store (Valkey 9, 1 primary + 2 replicas, min-replicas-to-write 1)",
            engine="valkey", engine_version="9.0",
            cache_node_type=spec["valkey_node_type"],
            num_cache_clusters=3,
            automatic_failover_enabled=True, multi_az_enabled=True,
            cache_parameter_group_name=params.ref,
            cache_subnet_group_name=subnets.ref,
            security_group_ids=[sg.security_group_id],
            at_rest_encryption_enabled=True,
            transit_encryption_enabled=True, transit_encryption_mode="required",
            auth_token=auth.secret_value.unsafe_unwrap(),
            snapshot_retention_limit=1,
        )
        group.cfn_options.deletion_policy = cdk.CfnDeletionPolicy.SNAPSHOT
        group.cfn_options.update_replace_policy = cdk.CfnDeletionPolicy.SNAPSHOT
        return auth, group

    # ---- EFS（chatgpt/ のトークンを全 Pod で共有） ----
    def _efs(self, spec: dict, vpc: ec2.Vpc) -> efs.FileSystem:
        sg = ec2.SecurityGroup(self, "EfsSg", vpc=vpc, description="EFS (lab)")
        sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(2049), "from inside the VPC")
        return efs.FileSystem(
            self, "Efs", vpc=vpc, security_group=sg, encrypted=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            lifecycle_policy=efs.LifecyclePolicy.AFTER_30_DAYS,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )


class ComputeStack(cdk.Stack):
    """EKS Auto Mode のクラスタ。消して作り直せる（S3、Aurora などは data に残る）。"""

    def __init__(self, scope: Construct, id_: str, *, spec: dict, vpc: ec2.Vpc, data: DataStack, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)
        prefix = spec["prefix"]
        cluster_role = iam.Role(
            self, "ClusterRole", role_name=f"{prefix}-eks-cluster",
            assumed_by=iam.ServicePrincipal("eks.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(n) for n in (
                "AmazonEKSClusterPolicy", "AmazonEKSComputePolicy", "AmazonEKSBlockStoragePolicyV2",
                "AmazonEKSLoadBalancingPolicy", "AmazonEKSNetworkingPolicy")])
        cluster_role.assume_role_policy.add_statements(iam.PolicyStatement(
            actions=["sts:TagSession"], principals=[iam.ServicePrincipal("eks.amazonaws.com")]))
        node_role = iam.Role(
            self, "NodeRole", role_name=f"{prefix}-eks-node",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(n) for n in (
                "AmazonEKSWorkerNodeMinimalPolicy", "AmazonEC2ContainerRegistryPullOnly")])

        cluster = eks.CfnCluster(
            self, "Cluster",
            name=spec["cluster_name"],
            version=spec["kubernetes_version"],
            role_arn=cluster_role.role_arn,
            resources_vpc_config=eks.CfnCluster.ResourcesVpcConfigProperty(
                subnet_ids=[s.subnet_id for s in vpc.private_subnets],
                endpoint_public_access=True, endpoint_private_access=True),
            access_config=eks.CfnCluster.AccessConfigProperty(
                authentication_mode="API", bootstrap_cluster_creator_admin_permissions=False),
            bootstrap_self_managed_addons=False,
            # Auto Mode: ノード、EBS、ALB を AWS が受け持つ。組み込みのノードプールは system だけ
            # （default の NodeClass を用意させる）。一般の Pod は labtool が入れる arm64 のノードプールに載る
            compute_config=eks.CfnCluster.ComputeConfigProperty(
                enabled=True, node_pools=["system"], node_role_arn=node_role.role_arn),
            storage_config=eks.CfnCluster.StorageConfigProperty(
                block_storage=eks.CfnCluster.BlockStorageProperty(enabled=True)),
            kubernetes_network_config=eks.CfnCluster.KubernetesNetworkConfigProperty(
                elastic_load_balancing=eks.CfnCluster.ElasticLoadBalancingProperty(enabled=True)),
            logging=eks.CfnCluster.LoggingProperty(cluster_logging=eks.CfnCluster.ClusterLoggingProperty(
                enabled_types=[eks.CfnCluster.LoggingTypeConfigProperty(type=t) for t in ("api", "audit")])),
            upgrade_policy=eks.CfnCluster.UpgradePolicyProperty(support_type="STANDARD"),
        )
        cluster.node.add_dependency(cluster_role)
        cluster.node.add_dependency(node_role)

        # 操作者（./lab を流す IAM の主体）にクラスタ管理者の権限
        eks.CfnAccessEntry(
            self, "OperatorAccess", cluster_name=cluster.ref, principal_arn=spec["operator_principal_arn"],
            type="STANDARD",
            access_policies=[eks.CfnAccessEntry.AccessPolicyProperty(
                policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                access_scope=eks.CfnAccessEntry.AccessScopeProperty(type="cluster"))])

        # Pod Identity: ServiceAccount ↔ IAM ロール（labtool/aws.py の POD_IDENTITIES と同じ表）
        for key, target in spec["pod_identities"].items():
            if key == "efs-csi":
                continue   # EFS CSI はアドオンの設定で結び付ける
            eks.CfnPodIdentityAssociation(
                self, f"PodIdentity{key.title().replace('-', '')}", cluster_name=cluster.ref,
                namespace=target["namespace"], service_account=target["service_account"],
                role_arn=data.roles[key].role_arn)

        # EFS CSI ドライバー（chatgpt/ のトークンの共有ボリューム）。Auto Mode には入っていないのでアドオンで足す
        efs_target = spec["pod_identities"]["efs-csi"]
        eks.CfnAddon(
            self, "EfsCsi", cluster_name=cluster.ref, addon_name="aws-efs-csi-driver",
            resolve_conflicts="OVERWRITE",
            pod_identity_associations=[eks.CfnAddon.PodIdentityAssociationProperty(
                role_arn=data.roles["efs-csi"].role_arn, service_account=efs_target["service_account"])])

        cdk.CfnOutput(self, "ClusterName", value=cluster.ref)
        cdk.CfnOutput(self, "ClusterEndpoint", value=cluster.attr_endpoint)
