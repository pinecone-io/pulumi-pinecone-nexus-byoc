"""PineconeAWSCluster - main component for AWS BYOC deployments."""

import json
from dataclasses import dataclass, field

import pulumi
import pulumi_aws as aws

from ..common import api
from ..common.cred_refresher import RegistryCredentialRefresher
from ..common.k8s_configmaps import K8sConfigMaps
from ..common.k8s_secrets import K8sSecrets, NexusSecretConfig
from ..common.naming import cell_name as _cell_name
from ..common.nexus import Nexus, NexusBlobStorage, NexusConfig, derive_api_key_refs
from ..common.nexus_uninstaller import NexusUninstaller
from ..common.pinetools import Pinetools
from ..common.providers import (
    DATADOG_DISABLED_PLACEHOLDER,
    DEFAULT_WORKSPACE_NAME,
    AmpAccess,
    AmpAccessArgs,
    ApiKey,
    ApiKeyArgs,
    CpgwApiKey,
    CpgwApiKeyArgs,
    DatadogApiKey,
    DatadogApiKeyArgs,
    DefaultWorkspace,
    DefaultWorkspaceArgs,
    Environment,
    EnvironmentArgs,
    ServiceAccount,
    ServiceAccountArgs,
)
from ..common.registry import AWS_REGISTRY, NEXUS_AWS_REGISTRY
from ..common.uninstaller import ClusterUninstaller
from .dns import DNS
from .eks import EKS
from .k8s_addons import K8sAddons
from .nexus_s3 import NexusS3Buckets
from .nlb import NLB
from .pulumi_operator import PulumiOperator
from .rds import RDS, RDSInstance
from .s3 import S3Buckets
from .vpc import VPC


@dataclass
class NodePool:
    name: str
    instance_type: str = "r6in.large"
    min_size: int = 1
    max_size: int = 10
    desired_size: int = 3
    disk_size_gb: int = 100
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)


@dataclass
class PineconeAWSClusterArgs:
    # required
    pinecone_api_key: pulumi.Input[str]
    pinecone_version: str

    # aws specific
    region: str = "us-east-1"
    availability_zones: list[str] = field(default_factory=lambda: ["us-east-1a", "us-east-1b"])

    # networking
    vpc_cidr: str = "10.0.0.0/16"

    # kubernetes
    kubernetes_version: str = "1.33"
    node_pools: list[NodePool] | None = None

    # dns
    parent_dns_zone_name: str = "byoc.pinecone.io"

    # features
    public_access_enabled: bool = True  # false = private access only via privatelink
    deletion_protection: bool = True  # protect RDS and S3 from accidental deletion
    # Set to a NexusConfig to deploy Nexus alongside the DB stack. None = DB-only.
    nexus: NexusConfig | None = None

    # pinecone specific
    api_url: str = "https://api.pinecone.io"
    global_env: str = "prod"
    auth0_domain: str = "https://login.pinecone.io"
    # Base URL of the Pinecone web console (workspace deep links). Override
    # for preprod/internal installs.
    console_url: str = "https://app.pinecone.io"
    # gcp_project is needed by some helmfiles even for AWS clusters (cross-cloud monitoring/metrics)
    gcp_project: str = "production-pinecone"

    # custom AMI
    custom_ami_id: str | None = None

    # KMS key ARN for encrypting S3 and RDS
    kms_key_arn: str | None = None

    # tags
    tags: dict[str, str] | None = None

    def __post_init__(self):
        if self.nexus is not None and self.nexus.fdb_mode == "external":
            raise ValueError(
                "nexus.fdb_mode='external' (shared data-plane FDB cluster) is not "
                "wired on AWS yet; Nexus on AWS runs its own single-pod FDB "
                "(fdb_mode='single')."
            )


class PineconeAWSCluster(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        args: PineconeAWSClusterArgs,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:PineconeAWSCluster", name, None, opts)

        self.args = args
        child_opts = pulumi.ResourceOptions(parent=self)
        config = self._build_config(args)
        self._config = config

        self._environment = Environment(
            f"{config.resource_prefix}-environment",
            EnvironmentArgs(
                cloud="aws",
                region=args.region,
                global_env=args.global_env,
                api_url=args.api_url,
                secret=args.pinecone_api_key,
                is_public_endpoint_enabled=args.public_access_enabled,
                is_nexus_enabled=args.nexus is not None,
            ),
            opts=child_opts,
        )

        self._cell_name = _cell_name(self._environment)

        # resource_suffix for unique AWS resource names (last 4 chars of cell_name)
        self._resource_suffix = self._cell_name.apply(lambda cn: cn[-4:])

        self._cpgw_api_key = CpgwApiKey(
            f"{config.resource_prefix}-cpgw-api-key",
            CpgwApiKeyArgs(
                environment=self._environment.env_name,
                api_url=args.api_url,
                pinecone_api_key=args.pinecone_api_key,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._environment]),
        )

        self._service_account = ServiceAccount(
            f"{config.resource_prefix}-service-account",
            ServiceAccountArgs(
                name=self._cell_name.apply(lambda cn: f"{cn}-sa"),
                api_url=args.api_url,
                secret=self._cpgw_api_key.key,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        self._api_key = ApiKey(
            f"{config.resource_prefix}-api-key",
            ApiKeyArgs(
                org_id=self._environment.org_id,
                project_name="__SLI__",
                key_name=self._cell_name.apply(lambda cn: f"{cn}-key"),
                api_url=args.api_url,
                auth0_domain=args.auth0_domain,
                auth0_client_id=self._service_account.client_id,
                auth0_client_secret=self._service_account.client_secret,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._service_account]),
        )

        if config.datadog_enabled:
            self._datadog_api_key = DatadogApiKey(
                f"{config.resource_prefix}-datadog-api-key",
                DatadogApiKeyArgs(
                    api_url=args.api_url,
                    cpgw_api_key=self._cpgw_api_key.key,
                ),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
            )
        else:
            self._datadog_api_key = None

        self._vpc = VPC(f"{config.resource_prefix}-vpc", config, opts=child_opts)

        self._eks = EKS(
            f"{config.resource_prefix}-eks",
            config,
            self._vpc,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vpc]),
        )

        self._s3 = S3Buckets(
            f"{config.resource_prefix}-s3",
            config,
            cell_name=self._cell_name,
            kms_key_arn=args.kms_key_arn,
            force_destroy=not args.deletion_protection,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._eks]),
        )

        # storage integration role for data-importer S3 access
        caller_identity = aws.get_caller_identity()
        assume_role_policy = pulumi.Output.from_input(caller_identity.account_id).apply(
            lambda account_id: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": {
                        "Sid": "AllowAccountRoles",
                        "Effect": "Allow",
                        "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                        "Action": "sts:AssumeRole",
                    },
                }
            )
        )
        self._storage_integration_role = aws.iam.Role(
            f"{config.resource_prefix}-storage-integration-role",
            name=self._resource_suffix.apply(
                lambda s: f"{config.resource_prefix}-storage-integration-{s}"
            ),
            assume_role_policy=assume_role_policy,
            tags=config.tags(Name=f"{config.resource_prefix}-storage-integration"),
            opts=child_opts,
        )
        aws.iam.RolePolicy(
            f"{config.resource_prefix}-storage-integration-policy",
            role=self._storage_integration_role.id,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:ListBucket",
                                "s3:GetObject",
                            ],
                            "Resource": "*",
                        }
                    ],
                }
            ),
            opts=child_opts,
        )
        # allow ec2 node role to assume storage integration role (for data-importer)
        aws.iam.RolePolicy(
            f"{config.resource_prefix}-ec2-allow-assume-role",
            role=self._eks.node_role_name,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Resource": "arn:aws:iam::*:role/*",
                        },
                    ],
                }
            ),
            opts=child_opts,
        )

        self._subdomain = self._environment.env_name

        self._dns = DNS(
            f"{config.resource_prefix}-dns",
            subdomain=self._subdomain.apply(lambda name: name.removesuffix(".byoc")),
            parent_zone_name=args.parent_dns_zone_name,
            api_url=args.api_url,
            cpgw_api_key=self._cpgw_api_key.key,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        self._rds = RDS(
            f"{config.resource_prefix}-rds",
            config,
            self._vpc,
            cell_name=self._cell_name,
            kms_key_arn=args.kms_key_arn,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vpc]),
        )

        self._k8s_addons = K8sAddons(
            f"{config.resource_prefix}-k8s-addons",
            config,
            self._eks,
            self._vpc.vpc_id,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._eks]),
        )

        self._amp_access = AmpAccess(
            f"{config.resource_prefix}-amp-access",
            AmpAccessArgs(
                workload_role_arn=self._k8s_addons.amp_ingest_role.arn,
                api_url=args.api_url,
                cpgw_api_key=self._cpgw_api_key.key,
            ),
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[self._cpgw_api_key, self._k8s_addons]
            ),
        )

        aws.iam.RolePolicy(
            f"{config.resource_prefix}-amp-allow-assume-pinecone-role",
            role=self._k8s_addons.amp_ingest_role.id,
            policy=self._amp_access.pinecone_role_arn.apply(
                lambda arn: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Resource": arn,
                            }
                        ],
                    }
                )
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._amp_access]),
        )

        self._nlb = NLB(
            f"{config.resource_prefix}-nlb",
            config,
            self._vpc,
            self._dns,
            k8s_provider=self._eks.provider,
            cluster_security_group_id=self._eks.cluster_security_group_id,
            cell_name=self._cell_name,
            public_access_enabled=args.public_access_enabled,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self._vpc, self._dns, self._eks, self._k8s_addons],
            ),
        )

        self._k8s_secrets = K8sSecrets(
            f"{config.resource_prefix}-k8s-secrets",
            k8s_provider=self._eks.provider,
            cpgw_api_key=self._cpgw_api_key.key,
            gcps_api_key=self._api_key.value,
            dd_api_key=(
                self._datadog_api_key.api_key
                if self._datadog_api_key is not None
                else DATADOG_DISABLED_PLACEHOLDER
            ),
            nexus=NexusSecretConfig(
                api_key=args.pinecone_api_key,
                provider_keys=args.nexus.provider_keys,
                provider_key_refs=(
                    derive_api_key_refs(args.nexus.inference_models_toml)
                    if args.nexus.inference_models_toml is not None
                    else None
                ),
            )
            if args.nexus is not None
            else None,
            control_db=self._rds.control_db,
            system_db=self._rds.system_db,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    r
                    for r in [
                        self._eks,
                        self._api_key,
                        self._datadog_api_key,
                        self._rds,
                    ]
                    if r is not None
                ],
            ),
        )

        self._pulumi_operator = PulumiOperator(
            f"{config.resource_prefix}-pulumi-operator",
            config,
            oidc_provider_arn=self._eks.oidc_provider_arn,
            oidc_provider_url=self._eks.oidc_provider_url,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._eks]),
        )

        aws.iam.RolePolicy(
            f"{config.resource_prefix}-ec2-allow-kms",
            role=self._eks.node_role_name,
            policy=self._pulumi_operator.kms_key_arn.apply(
                lambda kms_arn: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "kms:Encrypt",
                                    "kms:Decrypt",
                                    "kms:GenerateDataKey",
                                    "kms:DescribeKey",
                                ],
                                "Resource": kms_arn,
                            }
                        ],
                    }
                )
            ),
            opts=child_opts,
        )

        if args.kms_key_arn:
            aws.iam.RolePolicy(
                f"{config.resource_prefix}-ec2-allow-customer-kms",
                role=self._eks.node_role_name,
                policy=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "kms:Encrypt",
                                    "kms:Decrypt",
                                    "kms:ReEncrypt*",
                                    "kms:GenerateDataKey*",
                                    "kms:DescribeKey",
                                ],
                                "Resource": args.kms_key_arn,
                            }
                        ],
                    }
                ),
                opts=child_opts,
            )

        pulumi_outputs = {
            "cell_name": self._cell_name,
            "org_name": self._environment.org_name,
            "cloud": "aws",
            "region": args.region,
            "global_env": args.global_env,
            "subdomain": self._subdomain,
            "availability_zones": args.availability_zones,
            "certificate_arn": self._dns.certificate_arn,
            "dns_zone_id": self._dns.zone_id,
            "private_endpoint_certificate_arn": self._dns.certificate_arn,
            "aws_k8s_version": args.kubernetes_version,
            "aws_ec2_iam_role_arn": self._eks.node_role_arn,
            "aws_subnet_ids": self._vpc.private_subnet_ids,
            "image_registry": AWS_REGISTRY.base_url,
            "gcp_project": args.gcp_project,
            "sli_checkers_project_id": self._api_key.project_id,
            "aws_storage_integration_role_arn": self._storage_integration_role.arn,
            "customer_tags": args.tags or {},
            "public_access_enabled": args.public_access_enabled,
            # Enable the netstack *.wksp route only when Nexus is wired -- the same
            # condition that turns on gateway.workspaceAuth (common/nexus.py), so
            # routing and its auth edge go live in lockstep and never on a
            # non-Nexus BYOC cell (which has no nexus-gateway to route to).
            "workspace_routing_enabled": args.nexus is not None,
            "external_dns_role_arn": self._k8s_addons.external_dns_role.arn,
            "pulumi_backend_url": self._pulumi_operator.backend_url,
            "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
            "pulumi_operator_role_arn": self._pulumi_operator.operator_role_arn,
            "aws_amp_region": self._amp_access.amp_region,
            "aws_amp_remote_write_url": self._amp_access.amp_remote_write_endpoint,
            "aws_amp_sigv4_role_arn": self._amp_access.pinecone_role_arn,
            "aws_amp_ingest_role_arn": self._k8s_addons.amp_ingest_role.arn,
            "base64_encoded_user_data": self._eks.base64_encoded_user_data,
            "custom_ami_id": args.custom_ami_id,
        }

        self._k8s_configmaps = K8sConfigMaps(
            f"{config.resource_prefix}-k8s-configmaps",
            k8s_provider=self._eks.provider,
            cloud="aws",
            cell_name=self._cell_name,
            env=args.global_env,
            is_prod=args.global_env == "prod",
            domain=self._subdomain,
            region=args.region,
            public_access_enabled=args.public_access_enabled,
            pulumi_outputs=pulumi_outputs,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[self._eks, self._dns, self._s3, self._rds]
            ),
        )

        self._ecr_refresher = RegistryCredentialRefresher(
            f"{config.resource_prefix}-ecr-refresher",
            k8s_provider=self._eks.provider,
            cpgw_url=args.api_url,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._k8s_secrets]),
        )

        self._pinetools = Pinetools(
            f"{config.resource_prefix}-pinetools",
            k8s_provider=self._eks.provider,
            pinecone_version=args.pinecone_version,
            pinetools_image=AWS_REGISTRY.pinetools_image(args.pinecone_version),
            config_map_dependencies=self._k8s_configmaps.config_maps,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._eks, self._k8s_configmaps]),
        )

        # Install Nexus after the DB stack is ready.
        self._nexus = None
        self._nexus_s3 = None
        self._nexus_project_id = None
        self._default_workspace = None
        self.__default_workspace_exists = None
        if args.nexus is not None:
            nx = args.nexus
            # Nexus versions independently of the DB stack (separate repo, separate
            # image tags), so there is no meaningful fallback to pinecone_version --
            # a DB tag never names a nexus_deploy/nexus_* image. Require it explicitly
            # rather than producing an unpullable image ref.
            if nx.version is None:
                raise ValueError(
                    "nexus.version must be set to the Nexus image tag (the nexus "
                    "images.yml build tag). It is unrelated to the DB pinecone_version."
                )
            # Durable S3 storage is always provisioned for AWS+Nexus (the `fs`
            # default isn't durable and file upload needs object storage). The
            # bucket prefix is derived from the cell name (`pc-nexus-{cell}`),
            # mirroring the GCP derivation -- the cell name is minted server-side
            # mid-deploy, so the operator can't supply it in advance.
            # `storage_bucket_prefix` is an optional override, not a gate.
            storage_prefix = nx.storage_bucket_prefix or self._cell_name.apply(
                lambda cn: f"pc-nexus-{cn}"
            )
            self._nexus_s3 = NexusS3Buckets(
                f"{config.resource_prefix}-nexus-s3",
                config,
                cell_name=self._cell_name,
                prefix=storage_prefix,
                oidc_provider_arn=self._eks.oidc_provider_arn,
                oidc_provider_url=self._eks.oidc_provider_url,
                kms_key_arn=args.kms_key_arn,
                force_destroy=not args.deletion_protection,
                # The IRSA trust policy references the cluster's OIDC provider,
                # so the EKS component (which creates it) must exist first.
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._eks]),
            )
            blob_storage = NexusBlobStorage(
                source=self._nexus_s3.source,
                knowledge=self._nexus_s3.knowledge,
                archive=self._nexus_s3.archive,
            )
            # Annotate the Nexus KSAs so the pods assume the S3 role via IRSA.
            nexus_sa_annotations = self._nexus_s3.role_arn.apply(
                lambda arn: {"eks.amazonaws.com/role-arn": arn}
            )
            self._nexus_project_id = nx.byoc_project_id or self._api_key.project_id
            self._nexus = Nexus(
                f"{config.resource_prefix}-nexus",
                k8s_provider=self._eks.provider,
                image_registry=(nx.image_registry or NEXUS_AWS_REGISTRY.base_url),
                nexus_version=nx.version,
                byoc_env=nx.byoc_env or self._environment.env_name,
                cloud="aws",
                region=args.region,
                pinecone_prod=args.global_env == "prod",
                byoc_project_id=self._nexus_project_id,
                byoc_vault_id=(
                    nx.byoc_vault_id or self._resource_suffix.apply(lambda s: f"byoc{s}")
                ),
                # gp3 via the EBS CSI addon (installed in K8sAddons).
                storage_class="gp3",
                # No gateway Ingress: nothing on EKS consumes one. The gateway is
                # reached through the netstack *.wksp route via the Gloo edge
                # (nexus#1362) -- the in-cluster nexus-gateway Service, not an
                # Ingress, is the routing target.
                ingress_class=None,
                blob_storage=blob_storage,
                service_account_annotations=nexus_sa_annotations,
                # CPGW index client: Nexus reaches the control-plane gateway at
                # {api_url}/internal/cpgw (synchronous CPS db_index_id on create).
                # Paired with the cpgw-api-key in the nexus-config secret.
                cpgw_api_url=f"{args.api_url}/internal/cpgw",
                inference_models_toml=nx.inference_models_toml,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[
                        r
                        for r in [
                            self._eks,
                            self._k8s_secrets,
                            self._k8s_configmaps,
                            self._ecr_refresher,
                            self._pinetools,
                            self._nlb,
                            self._nexus_s3,
                        ]
                        if r is not None
                    ],
                ),
            )
            # `helm uninstall` the Nexus releases on destroy (no Pulumi Release to
            # remove them now). Depends on the component so it runs while the
            # cluster, the nexus-deploy SA, and regcred still exist.
            self._nexus_uninstaller = NexusUninstaller(
                f"{config.resource_prefix}-nexus-uninstaller",
                kubeconfig=self._eks.kubeconfig.apply(json.dumps),
                deploy_image=self._nexus.deploy_image,
                cloud="aws",
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._nexus]),
            )

            # Depends on Nexus because the cell's operation poller is what
            # promotes the workspace to Ready — creating before it exists would
            # wait on nothing. First-run-only (no-op diff/delete): destroy
            # leaves the workspace; delete it via gCPS before teardown.
            self._default_workspace = DefaultWorkspace(
                f"{config.resource_prefix}-default-workspace",
                DefaultWorkspaceArgs(
                    name=DEFAULT_WORKSPACE_NAME,
                    environment=nx.byoc_env or self._environment.env_name,
                    api_url=args.api_url,
                    pinecone_api_key=args.pinecone_api_key,
                ),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._nexus]),
            )

        self._uninstaller = ClusterUninstaller(
            f"{config.resource_prefix}-uninstaller",
            kubeconfig=self._eks.kubeconfig.apply(json.dumps),
            pinetools_image=AWS_REGISTRY.pinetools_image(args.pinecone_version),
            cloud="aws",
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    self._pinetools.ns,
                    self._pinetools.sa,
                    self._pinetools.crb,
                    self._k8s_addons,
                    self._k8s_secrets,
                    self._k8s_configmaps,
                    self._ecr_refresher,
                    self._nlb,
                    self._pulumi_operator,
                ],
            ),
        )

        self.register_outputs(
            {
                "cluster_name": self._cell_name,
                "region": args.region,
                "organization_id": self._environment.org_id,
                "organization_name": self._environment.org_name,
                "vpc_id": self._vpc.vpc_id,
                "cluster_endpoint": self._eks.cluster.eks_cluster.endpoint,
                "kubeconfig": self._eks.kubeconfig,
                "data_bucket": self._s3.data_bucket_name,
                "control_db_endpoint": self._rds.control_db.endpoint,
                "system_db_endpoint": self._rds.system_db.endpoint,
                "certificate_arn": self._dns.certificate_arn,
                "environment_id": self._environment.id,
                "environment_name": self._environment.env_name,
                "service_account_id": self._service_account.id,
                "service_account_client_id": self._service_account.client_id,
                "api_key_project_id": self._api_key.project_id,
                "alb_controller_role_arn": self._k8s_addons.alb_controller_role.arn,
                "cluster_autoscaler_role_arn": self._k8s_addons.cluster_autoscaler_role.arn,
                "external_dns_role_arn": self._k8s_addons.external_dns_role.arn,
                "subdomain": self._subdomain,
                "sli_checkers_project_id": self._api_key.project_id,
                "cpgw_api_key": self._k8s_secrets.cpgw_api_key,
                "cpgw_admin_api_key_id": self._cpgw_api_key.key_id,
                "datadog_api_key_id": (
                    self._datadog_api_key.key_id if self._datadog_api_key is not None else None
                ),
                "customer_tags": args.tags or {},
                "pulumi_backend_url": self._pulumi_operator.backend_url,
                "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
                "storage_integration_role_arn": self._storage_integration_role.arn,
                "amp_region": self._amp_access.amp_region,
                "amp_remote_write_endpoint": self._amp_access.amp_remote_write_endpoint,
                "amp_sigv4_role_arn": self._amp_access.pinecone_role_arn,
                "amp_ingest_role_arn": self._k8s_addons.amp_ingest_role.arn,
                "vpc_endpoint_service_name": self._nlb.vpc_endpoint_service.service_name,
            }
        )

    def _build_config(self, args: PineconeAWSClusterArgs):
        # lazy import to avoid circular dependency: config imports are deferred
        from config.aws import AWSConfig, DatabaseConfig
        from config.base import NodePoolConfig, NodePoolTaint

        node_pools = []
        if args.node_pools:
            for np in args.node_pools:
                node_pools.append(
                    NodePoolConfig(
                        name=np.name,
                        instance_type=np.instance_type,
                        min_size=np.min_size,
                        max_size=np.max_size,
                        desired_size=np.desired_size,
                        disk_size_gb=np.disk_size_gb,
                        labels=np.labels,
                        taints=[
                            NodePoolTaint(key=t.key, value=t.value, effect=t.effect)
                            for t in np.taints
                        ],
                    )
                )
        else:
            node_pools = [
                NodePoolConfig(
                    name="default",
                    instance_type="r6in.large",
                    min_size=1,
                    max_size=10,
                    desired_size=3,
                    disk_size_gb=100,
                ),
            ]

        # Add Nexus node pools when enabled; DB-only deploys are unaffected.
        if args.nexus is not None:
            from .eks import nexus_node_pools

            node_pools.extend(nexus_node_pools())

        return AWSConfig(
            region=args.region,
            global_env=args.global_env,
            cloud="aws",
            availability_zones=args.availability_zones,
            vpc_cidr=args.vpc_cidr,
            kubernetes_version=args.kubernetes_version,
            node_pools=node_pools,
            parent_zone_name=args.parent_dns_zone_name,
            database=DatabaseConfig(deletion_protection=args.deletion_protection),
            custom_ami_id=args.custom_ami_id,
            kms_key_arn=args.kms_key_arn,
            custom_tags=args.tags or {},
        )

    @property
    def vpc_id(self) -> pulumi.Output[str]:
        return self._vpc.vpc_id

    @property
    def private_subnet_ids(self) -> list[pulumi.Output[str]]:
        return self._vpc.private_subnet_ids

    @property
    def public_subnet_ids(self) -> list[pulumi.Output[str]]:
        return self._vpc.public_subnet_ids

    @property
    def name(self) -> pulumi.Output[str]:
        return self._eks.cluster_name

    @property
    def cluster_endpoint(self) -> pulumi.Output[str]:
        return self._eks.cluster.eks_cluster.endpoint

    @property
    def kubeconfig(self) -> pulumi.Output:
        return self._eks.kubeconfig

    @property
    def k8s_provider(self) -> pulumi.ProviderResource:
        return self._eks.provider

    @property
    def oidc_provider_arn(self) -> pulumi.Output[str]:
        return self._eks.oidc_provider_arn

    @property
    def data_bucket_name(self) -> pulumi.Output[str]:
        return self._s3.data_bucket_name

    @property
    def data_bucket_arn(self) -> pulumi.Output[str]:
        return self._s3.data_bucket_arn

    @property
    def wal_bucket_name(self) -> pulumi.Output[str]:
        return self._s3.wal_bucket_name

    @property
    def control_db(self) -> RDSInstance:
        return self._rds.control_db

    @property
    def system_db(self) -> RDSInstance:
        return self._rds.system_db

    @property
    def control_db_endpoint(self) -> pulumi.Output[str]:
        return self._rds.control_db.endpoint

    @property
    def system_db_endpoint(self) -> pulumi.Output[str]:
        return self._rds.system_db.endpoint

    @property
    def control_db_connection_secret_arn(self) -> pulumi.Output[str]:
        return self._rds.control_db.connection_secret_arn

    @property
    def system_db_connection_secret_arn(self) -> pulumi.Output[str]:
        return self._rds.system_db.connection_secret_arn

    @property
    def certificate_arn(self) -> pulumi.Output[str]:
        return self._dns.certificate_arn

    @property
    def dns_zone_id(self) -> pulumi.Output[str]:
        return self._dns.zone_id

    @property
    def dns_name_servers(self) -> pulumi.Output[list]:
        return self._dns.name_servers

    @property
    def nlb_dns_name(self) -> pulumi.Output[str]:
        return self._nlb.nlb_dns_name

    @property
    def nlb_target_group_arn(self) -> pulumi.Output[str]:
        return self._nlb.target_group_arn

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def environment_id(self) -> pulumi.Output[str]:
        return self._environment.id

    @property
    def environment_name(self) -> pulumi.Output[str]:
        return self._environment.env_name

    @property
    def service_account(self) -> ServiceAccount:
        return self._service_account

    @property
    def service_account_id(self) -> pulumi.Output[str]:
        return self._service_account.id

    @property
    def service_account_client_id(self) -> pulumi.Output[str]:
        return self._service_account.client_id

    @property
    def service_account_client_secret(self) -> pulumi.Output[str]:
        return self._service_account.client_secret

    @property
    def api_key(self) -> ApiKey:
        return self._api_key

    @property
    def api_key_value(self) -> pulumi.Output[str]:
        return self._api_key.value

    @property
    def api_key_project_id(self) -> pulumi.Output[str]:
        return self._api_key.project_id

    @property
    def subdomain(self) -> pulumi.Output[str]:
        return self._subdomain

    @property
    def sli_checkers_project_id(self) -> pulumi.Output[str]:
        return self._api_key.project_id

    @property
    def cpgw_api_key(self) -> pulumi.Output[str]:
        return self._k8s_secrets.cpgw_api_key

    @property
    def customer_tags(self) -> dict[str, str]:
        return self.args.tags or {}

    @property
    def datadog_api_key(self) -> DatadogApiKey | None:
        return self._datadog_api_key

    @property
    def datadog_api_key_value(self) -> pulumi.Output[str] | None:
        if self._datadog_api_key is None:
            return None
        return self._datadog_api_key.api_key

    @property
    def datadog_api_key_id(self) -> pulumi.Output[str] | None:
        if self._datadog_api_key is None:
            return None
        return self._datadog_api_key.key_id

    @property
    def cpgw_admin_api_key(self) -> CpgwApiKey:
        return self._cpgw_api_key

    @property
    def cpgw_admin_api_key_id(self) -> pulumi.Output[str]:
        return self._cpgw_api_key.key_id

    @property
    def cpgw_admin_api_key_value(self) -> pulumi.Output[str]:
        return self._cpgw_api_key.key

    @property
    def pulumi_operator(self) -> PulumiOperator:
        return self._pulumi_operator

    @property
    def pulumi_backend_url(self) -> pulumi.Output[str]:
        return self._pulumi_operator.backend_url

    @property
    def pulumi_secrets_provider(self) -> pulumi.Output[str]:
        return self._pulumi_operator.secrets_provider

    @property
    def pulumi_operator_role_arn(self) -> pulumi.Output[str]:
        return self._pulumi_operator.operator_role_arn

    @property
    def amp_access(self) -> AmpAccess:
        return self._amp_access

    @property
    def amp_region(self) -> pulumi.Output[str]:
        return self._amp_access.amp_region

    @property
    def amp_remote_write_endpoint(self) -> pulumi.Output[str]:
        return self._amp_access.amp_remote_write_endpoint

    @property
    def amp_sigv4_role_arn(self) -> pulumi.Output[str]:
        return self._amp_access.pinecone_role_arn

    @property
    def amp_ingest_role_arn(self) -> pulumi.Output[str]:
        return self._k8s_addons.amp_ingest_role.arn

    @property
    def vpc_endpoint_service_name(self) -> pulumi.Output[str]:
        return self._nlb.vpc_endpoint_service.service_name

    @property
    def nexus(self) -> Nexus | None:
        return self._nexus

    @property
    def nexus_byoc_project_id(self) -> pulumi.Input[str] | None:
        """The BYOC single-tenant project id Nexus runs under, or None on DB-only deploys."""
        return self._nexus.byoc_project_id if self._nexus is not None else None

    @property
    def nexus_byoc_session_credential(self) -> pulumi.Output[str] | None:
        """The seeded BYOC login credential, or None on DB-only deploys. Marked secret."""
        return self._k8s_secrets.byoc_session_credential

    def _default_workspace_exists(self) -> pulumi.Output[bool]:
        """Live existence of the `default` workspace, checked once per program run.

        The bootstrap resource is first-run-only (never re-created, no-op
        delete), so its stored outputs outlive a workspace the user later
        deletes. The URL exports gate on this lookup instead, so they read
        null once the workspace is gone. Previews skip the network call and
        assume existence.
        """
        if self.__default_workspace_exists is None:
            workspace = self._default_workspace
            if workspace is None:
                # both URL properties return early on DB-only deploys, so this
                # is unreachable through them
                raise RuntimeError("default-workspace existence check requires a Nexus deploy")
            # unsecret: secretness taints everything derived from the API key,
            # which would render the exported URLs as [secret]; the boolean
            # reveals nothing about the key. The host input is purely for
            # sequencing — key and api_url resolve at program start, and on a
            # first deploy the check must not run before the workspace exists.
            # Previews run the same check so preview and update agree
            # (workspace_exists fails open, keeping offline previews working).
            self.__default_workspace_exists = pulumi.Output.unsecret(
                pulumi.Output.all(
                    self.args.pinecone_api_key,
                    self.args.api_url,
                    workspace.host,
                ).apply(lambda a: api.workspace_exists(a[0], a[1], DEFAULT_WORKSPACE_NAME))
            )
        return self.__default_workspace_exists

    @property
    def nexus_default_workspace_data_console_url(self) -> pulumi.Output[str] | None:
        """Console URL of the first-run `default` workspace.

        None on DB-only deploys; resolves to null once the workspace has been
        deleted (existence is re-checked on every `pulumi up`).
        """
        if self._default_workspace is None:
            return None
        # Built from the stored host, not the stored url: resource state is
        # frozen at creation, so anything persisted there can go stale. The
        # host is the durable fact; the path is decided at read time.
        url = pulumi.Output.concat("https://", self._default_workspace.host, "/contexts")
        return pulumi.Output.all(self._default_workspace_exists(), url).apply(
            lambda a: a[1] if a[0] else None
        )

    @property
    def nexus_default_workspace_control_console_url(self) -> pulumi.Output[str] | None:
        """Pinecone-console detail page for the default workspace.

        None on DB-only deploys; resolves to null once the workspace has been
        deleted (existence is re-checked on every `pulumi up`).
        """
        if self._default_workspace is None:
            return None
        url = pulumi.Output.concat(
            self.args.console_url,
            "/organizations/",
            self._environment.org_id,
            "/projects/",
            pulumi.Output.from_input(self._nexus_project_id),
            "/workspaces/",
            DEFAULT_WORKSPACE_NAME,
        )
        return pulumi.Output.all(self._default_workspace_exists(), url).apply(
            lambda a: a[1] if a[0] else None
        )
