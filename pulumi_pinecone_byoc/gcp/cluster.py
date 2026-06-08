"""PineconeGCPCluster - main component for BYOC deployments on GCP."""

from dataclasses import dataclass, field

import pulumi

from ..common.cred_refresher import RegistryCredentialRefresher
from ..common.k8s_configmaps import K8sConfigMaps
from ..common.k8s_secrets import K8sSecrets
from ..common.naming import cell_name as _cell_name
from ..common.nexus import Nexus
from ..common.pinetools import Pinetools
from ..common.providers import (
    AmpAccess,
    AmpAccessArgs,
    ApiKey,
    ApiKeyArgs,
    CpgwApiKey,
    CpgwApiKeyArgs,
    DatadogApiKey,
    DatadogApiKeyArgs,
    Environment,
    EnvironmentArgs,
    ServiceAccount,
    ServiceAccountArgs,
)
from ..common.registry import GCP_REGISTRY, NEXUS_GCP_REGISTRY
from ..common.uninstaller import ClusterUninstaller
from .alloydb import AlloyDB
from .dns import DNS
from .gcs import GCSBuckets
from .gke import GKE
from .k8s_addons import K8sAddons
from .nlb import InternalLoadBalancer
from .pulumi_operator import PulumiOperator
from .vpc import VPC


@dataclass
class NodePool:
    name: str
    machine_type: str = "n2-standard-4"
    min_size: int = 1
    max_size: int = 10
    disk_size_gb: int = 100
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)


@dataclass
class PineconeGCPClusterArgs:
    # required
    pinecone_api_key: pulumi.Input[str]
    pinecone_version: str

    # gcp specific
    project: str
    region: str = "us-central1"
    availability_zones: list[str] = field(
        default_factory=lambda: ["us-central1-a", "us-central1-b"]
    )

    # networking
    vpc_cidr: str = "10.112.0.0/12"

    # kubernetes
    kubernetes_version: str = "1.33"
    node_pools: list[NodePool] | None = None

    # dns
    parent_dns_zone_name: str = "byoc.pinecone.io"

    # features
    public_access_enabled: bool = True
    deletion_protection: bool = True
    # when True, enable Nexus alongside the DB stack: add the Nexus `services`/
    # `jobs` GKE node pools (task 2.1) and mint a deployment Pinecone key exposed
    # as a k8s secret (task 2.2). DB-only deploys leave this False.
    nexus_enabled: bool = False
    # image tag for the Nexus images (proposal §10 `nexus-version`; coordinated
    # with task 2.7). Only used when nexus_enabled. If left None, falls back to
    # `pinecone_version` so a combined manifest can pin a single coordinated tag
    # (the proposal §0/§7 end-state is a single combined manifest pinning both
    # pinecone-version + nexus-version as one coordinated release).
    nexus_version: str | None = None
    # the `.byoc` deployment environment id surfaced to Nexus as
    # PINECONE_BYOC_ENV / chart `deployment.environment`. Only used when
    # nexus_enabled. If left None, the minted environment's env_name is used.
    nexus_byoc_env: pulumi.Input[str] | None = None
    # managed inference base for embed/rerank (INFERENCE_BASE). Only used when
    # nexus_enabled. If left None, defaults to `api_url` (PoC: inference is the
    # same managed endpoint as index CRUD — proposal §10).
    nexus_inference_base: pulumi.Input[str] | None = None
    # container registry base URL for the Nexus images. Nexus images live in a
    # separate Artifact Registry repo (nexus-alpha), NOT the DB `unstable` repo,
    # so this is independent of the DB registry. Only used when nexus_enabled. If
    # left None, defaults to NEXUS_GCP_REGISTRY.base_url (nexus-alpha).
    nexus_image_registry: str | None = None

    # pinecone specific
    api_url: str = "https://api.pinecone.io"
    global_env: str = "prod"
    auth0_domain: str = "https://login.pinecone.io"

    # cross-cloud: AWS account for AMP federation
    amp_aws_account_id: str = "713131977538"

    # tags/labels
    labels: dict[str, str] | None = None

    # workload identity - K8s service accounts that need GCS access
    writer_k8s_service_accounts: list[str] | None = None
    reader_k8s_service_accounts: list[str] | None = None


class PineconeGCPCluster(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        args: PineconeGCPClusterArgs,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:PineconeGCPCluster", name, None, opts)

        self.args = args
        child_opts = pulumi.ResourceOptions(parent=self)
        config = self._build_config(args)
        self._config = config

        self._environment = Environment(
            f"{config.resource_prefix}-environment",
            EnvironmentArgs(
                cloud="gcp",
                region=args.region,
                global_env=args.global_env,
                api_url=args.api_url,
                secret=args.pinecone_api_key,
                is_public_endpoint_enabled=args.public_access_enabled,
            ),
            opts=child_opts,
        )

        self._cell_name = _cell_name(self._environment)

        # resource_suffix for unique GCP resource names (last 4 chars of cell_name)
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

        self._datadog_api_key = DatadogApiKey(
            f"{config.resource_prefix}-datadog-api-key",
            DatadogApiKeyArgs(
                api_url=args.api_url,
                cpgw_api_key=self._cpgw_api_key.key,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        # Nexus deployment key (proposal §3.3, task 2.2): reuse the __SLI__
        # ApiKey minting pattern to mint one shared deployment key, handed to
        # Nexus as PINECONE_API_KEY. Gated on nexus_enabled so DB-only deploys
        # are unaffected.
        self._nexus_api_key = None
        if args.nexus_enabled:
            self._nexus_api_key = ApiKey(
                f"{config.resource_prefix}-nexus-api-key",
                ApiKeyArgs(
                    org_id=self._environment.org_id,
                    project_name="__SLI__",
                    key_name=self._cell_name.apply(lambda cn: f"{cn}-nexus-key"),
                    api_url=args.api_url,
                    auth0_domain=args.auth0_domain,
                    auth0_client_id=self._service_account.client_id,
                    auth0_client_secret=self._service_account.client_secret,
                ),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._service_account]),
            )

        self._vpc = VPC(
            f"{config.resource_prefix}-vpc",
            config,
            self._cell_name,
            opts=child_opts,
        )

        self._gke = GKE(
            f"{config.resource_prefix}-gke",
            config,
            self._vpc.network_id,
            self._vpc.main_subnet_id,
            self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vpc]),
        )

        self._gcs = GCSBuckets(
            f"{config.resource_prefix}-gcs",
            config,
            self._cell_name,
            force_destroy=not args.deletion_protection,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke]),
        )

        self._alloydb = AlloyDB(
            f"{config.resource_prefix}-alloydb",
            config,
            self._vpc.network_id,
            self._vpc.private_ip_range_name,
            self._vpc.private_connection,
            self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vpc]),
        )

        self._subdomain = self._environment.env_name

        self._dns = DNS(
            f"{config.resource_prefix}-dns",
            subdomain=self._subdomain.apply(lambda name: name.removesuffix(".byoc")),
            parent_zone_name=args.parent_dns_zone_name,
            api_url=args.api_url,
            cpgw_api_key=self._cpgw_api_key.key,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        self._k8s_addons = K8sAddons(
            f"{config.resource_prefix}-k8s-addons",
            self._gke,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke]),
        )

        self._nlb = InternalLoadBalancer(
            f"{config.resource_prefix}-nlb",
            config,
            self._gke.k8s_provider,
            self._vpc.psc_subnet_id,
            self._dns.dns_zone.name,
            self._dns.subdomain,
            self._cell_name,
            args.public_access_enabled,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self._vpc, self._dns, self._gke, self._k8s_addons],
            ),
        )

        self._k8s_secrets = K8sSecrets(
            f"{config.resource_prefix}-k8s-secrets",
            k8s_provider=self._gke.k8s_provider,
            cpgw_api_key=self._cpgw_api_key.key,
            gcps_api_key=self._api_key.value,
            dd_api_key=self._datadog_api_key.api_key,
            nexus_api_key=(self._nexus_api_key.value if self._nexus_api_key is not None else None),
            control_db=self._alloydb.control_db,
            system_db=self._alloydb.system_db,
            storage_integration_credentials=(
                {"key-json": self._gke.service_accounts.storage_integration_key_json}
                if self._gke.service_accounts.storage_integration_key_json is not None
                else None
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    r
                    for r in [
                        self._gke,
                        self._cpgw_api_key,
                        self._api_key,
                        self._datadog_api_key,
                        self._nexus_api_key,
                        self._alloydb,
                    ]
                    if r is not None
                ],
            ),
        )

        self._pulumi_operator = PulumiOperator(
            f"{config.resource_prefix}-pulumi-operator",
            config,
            self._gke.k8s_provider,
            self._gke.service_accounts.pulumi_sa.email,
            self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke]),
        )

        self._amp_access = AmpAccess(
            f"{config.resource_prefix}-amp-access",
            AmpAccessArgs(
                workload_role_arn=f"arn:aws:iam::{args.amp_aws_account_id}:user/AmpCpgwIamManagerUser",
                api_url=args.api_url,
                cpgw_api_key=self._cpgw_api_key.key,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        pulumi_outputs = {
            "cell_name": self._cell_name,
            "org_name": self._environment.org_name,
            "cloud": "gcp",
            "region": config.region,
            "global_env": config.global_env,
            "subdomain": self._subdomain,
            "availability_zones": config.availability_zones,
            "api_url": args.api_url,
            "dns_zone_name": self._dns.dns_zone.name,
            "gcp_k8s_version": args.kubernetes_version,
            "gcp_project": config.project,
            "image_registry": GCP_REGISTRY.base_url,
            "sli_checkers_project_id": self._api_key.project_id,
            "customer_tags": args.labels or {},
            "public_access_enabled": args.public_access_enabled,
            "pulumi_backend_url": self._pulumi_operator.backend_url,
            "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
            "aws_amp_region": self._amp_access.amp_region,
            "aws_amp_remote_write_url": self._amp_access.amp_remote_write_endpoint,
            "aws_amp_sigv4_role_arn": self._amp_access.pinecone_role_arn,
            "aws_amp_ingest_role_arn": "",
            "gcp_np_sa_email": self._gke.service_accounts.nodepool_sa.email,
            "gcp_read_sa_email": self._gke.service_accounts.reader_sa.email,
            "gcp_write_sa_email": self._gke.service_accounts.writer_sa.email,
            "gcp_dns_sa_email": self._gke.service_accounts.dns_sa.email,
            "gcp_pulumi_sa_email": self._gke.service_accounts.pulumi_sa.email,
            "gcp_write_sa_id": self._gke.service_accounts.writer_sa.account_id,
            "gcp_read_sa_id": self._gke.service_accounts.reader_sa.account_id,
            "gcp_dns_sa_id": self._gke.service_accounts.dns_sa.account_id,
        }

        self._k8s_configmaps = K8sConfigMaps(
            f"{config.resource_prefix}-k8s-configmaps",
            k8s_provider=self._gke.k8s_provider,
            cloud="gcp",
            cell_name=self._cell_name,
            env=config.global_env,
            is_prod=config.global_env == "prod",
            domain=self._subdomain,
            region=config.region,
            public_access_enabled=args.public_access_enabled,
            pulumi_outputs=pulumi_outputs,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self._gke, self._dns, self._gcs, self._alloydb],
            ),
        )

        self._gcr_refresher = RegistryCredentialRefresher(
            f"{config.resource_prefix}-gcr-refresher",
            k8s_provider=self._gke.k8s_provider,
            cpgw_url=args.api_url,
            registry=GCP_REGISTRY.type,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._k8s_secrets]),
        )

        self._pinetools = Pinetools(
            f"{config.resource_prefix}-pinetools",
            k8s_provider=self._gke.k8s_provider,
            pinecone_version=args.pinecone_version,
            pinetools_image=GCP_REGISTRY.pinetools_image(args.pinecone_version),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke, self._k8s_configmaps]),
        )

        # Nexus stack (proposal §4, tasks 2.4-2.6). Installed AFTER the DB
        # stack: depends_on the Pinetools install (DB platform bootstrap), the
        # k8s secrets (minted Pinecone key in the `nexus` namespace) and the
        # regcred refresher (image pull auth in nexus/nexus-tasks). Gated on
        # nexus_enabled so DB-only deploys are byte-for-byte unaffected.
        self._nexus = None
        if args.nexus_enabled:
            self._nexus = Nexus(
                f"{config.resource_prefix}-nexus",
                k8s_provider=self._gke.k8s_provider,
                # Nexus images live in nexus-alpha, NOT the DB `unstable` repo.
                image_registry=(args.nexus_image_registry or NEXUS_GCP_REGISTRY.base_url),
                # coordinated `nexus-version` (task 2.7); fall back to the DB
                # version so a single combined manifest works until 2.7 lands.
                nexus_version=args.nexus_version or args.pinecone_version,
                # managed control-plane base; index CRUD + inference stay managed.
                pinecone_api_base=args.api_url,
                # the `.byoc` deployment env id (chart `deployment.environment`);
                # default to the minted environment's name when unset (task 2.7).
                byoc_env=args.nexus_byoc_env or self._environment.env_name,
                # managed inference base (INFERENCE_BASE); the component defaults
                # it to pinecone_api_base when None (PoC: same managed endpoint).
                inference_base=args.nexus_inference_base,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[
                        self._gke,
                        self._k8s_secrets,
                        self._k8s_configmaps,
                        self._gcr_refresher,
                        self._pinetools,
                        self._nlb,
                    ],
                ),
            )

        self._uninstaller = ClusterUninstaller(
            f"{config.resource_prefix}-uninstaller",
            kubeconfig=self._gke.kubeconfig,
            pinetools_image=GCP_REGISTRY.pinetools_image(args.pinecone_version),
            cloud="gcp",
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    self._pinetools.ns,
                    self._pinetools.sa,
                    self._pinetools.crb,
                    self._k8s_addons,
                    self._k8s_secrets,
                    self._k8s_configmaps,
                    self._gcr_refresher,
                    self._nlb,
                    self._pulumi_operator,
                ],
            ),
        )

        self.register_outputs(
            {
                "cluster_name": self._cell_name,
                "region": config.region,
                "organization_id": self._environment.org_id,
                "organization_name": self._environment.org_name,
                "vpc_id": self._vpc.network_id,
                "cluster_endpoint": self._gke.cluster.endpoint,
                "kubeconfig": self._gke.kubeconfig,
                "data_bucket": self._gcs.data_bucket.name,
                "control_db_endpoint": self._alloydb.control_db.endpoint,
                "system_db_endpoint": self._alloydb.system_db.endpoint,
                "environment_id": self._environment.id,
                "environment_name": self._environment.env_name,
                "service_account_id": self._service_account.id,
                "service_account_client_id": self._service_account.client_id,
                "api_key_project_id": self._api_key.project_id,
                "subdomain": self._subdomain,
                "sli_checkers_project_id": self._api_key.project_id,
                "cpgw_api_key": self._k8s_secrets.cpgw_api_key,
                "cpgw_admin_api_key_id": self._cpgw_api_key.key_id,
                "datadog_api_key_id": self._datadog_api_key.key_id,
                "customer_tags": config.custom_tags,
                "pulumi_backend_url": self._pulumi_operator.backend_url,
                "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
                "psc_service_attachment": self._nlb.service_attachment.self_link,
            }
        )

    def _build_config(self, args: PineconeGCPClusterArgs):
        # lazy import to avoid circular dependency: config imports are deferred
        from config.base import NodePoolConfig
        from config.gcp import AlloyDBConfig, AlloyDBInstanceConfig, GCPConfig

        node_pools = []
        if args.node_pools:
            for np in args.node_pools:
                node_pools.append(
                    NodePoolConfig(
                        name=np.name,
                        machine_type=np.machine_type,
                        min_size=np.min_size,
                        max_size=np.max_size,
                        disk_size_gb=np.disk_size_gb,
                        labels=np.labels,
                        taints=np.taints,
                    )
                )
        else:
            node_pools = [
                NodePoolConfig(
                    name="default",
                    machine_type="n2-standard-4",
                    min_size=1,
                    max_size=10,
                    disk_size_gb=100,
                ),
            ]

        # Nexus schedules onto dedicated `services`/`jobs` pools (labels + taints
        # matching the chart). Added only when Nexus is enabled so DB-only deploys
        # are unaffected. See gke.nexus_node_pools.
        if args.nexus_enabled:
            from .gke import nexus_node_pools

            node_pools.extend(nexus_node_pools())

        control_db_cpu = 2
        system_db_cpu = 2

        config = GCPConfig(
            project=args.project,
            global_env=args.global_env,
            cloud="gcp",
            region=args.region,
            availability_zones=args.availability_zones,
            vpc_cidr=args.vpc_cidr,
            kubernetes_version=args.kubernetes_version,
            parent_zone_name=args.parent_dns_zone_name,
            node_pools=node_pools,
            nexus_enabled=args.nexus_enabled,
            database=AlloyDBConfig(
                control_db=AlloyDBInstanceConfig(
                    name="control-db",
                    cpu_count=control_db_cpu,
                    username="controldb",
                    db_name="controldb",
                ),
                system_db=AlloyDBInstanceConfig(
                    name="system-db",
                    cpu_count=system_db_cpu,
                    username="systemdb",
                    db_name="systemdb",
                ),
                deletion_protection=args.deletion_protection,
            ),
            custom_tags=args.labels or {},
        )

        if args.writer_k8s_service_accounts is not None:
            config.writer_k8s_service_accounts = args.writer_k8s_service_accounts
        if args.reader_k8s_service_accounts is not None:
            config.reader_k8s_service_accounts = args.reader_k8s_service_accounts

        return config

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def name(self) -> pulumi.Output[str]:
        return self._gke.cluster.name

    @property
    def vpc(self) -> VPC:
        return self._vpc

    @property
    def gke(self) -> GKE:
        return self._gke

    @property
    def gcs(self) -> GCSBuckets:
        return self._gcs

    @property
    def alloydb(self) -> AlloyDB:
        return self._alloydb

    @property
    def dns(self) -> DNS:
        return self._dns

    @property
    def data_bucket(self) -> pulumi.Output[str]:
        return self._gcs.data_bucket.name

    @property
    def psc_service_attachment(self) -> pulumi.Output[str]:
        return self._nlb.service_attachment.self_link

    @property
    def nexus(self) -> Nexus | None:
        return self._nexus
