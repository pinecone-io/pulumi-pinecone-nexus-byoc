"""PineconeGCPCluster - main component for BYOC deployments on GCP."""

from dataclasses import dataclass, field

import pulumi

from ..common import api
from ..common.cred_refresher import RegistryCredentialRefresher
from ..common.k8s_configmaps import K8sConfigMaps
from ..common.k8s_secrets import K8sSecrets, NexusSecretConfig
from ..common.naming import cell_name as _cell_name
from ..common.naming import default_workspace_name
from ..common.nexus import (
    Nexus,
    NexusBlobStorage,
    NexusConfig,
    derive_api_key_refs,
    require_byoc_project_id,
    require_external_fdb_for_nexus,
)
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
from ..common.registry import GCP_REGISTRY, NEXUS_GCP_REGISTRY
from ..common.uninstaller import ClusterUninstaller
from .alloydb import AlloyDB
from .dns import DNS
from .gcs import GCSBuckets
from .gke import GKE
from .k8s_addons import K8sAddons
from .nexus_gcs import NexusGCSBuckets
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
        default_factory=lambda: ["us-central1-a", "us-central1-b", "us-central1-c"]
    )

    # networking
    vpc_cidr: str = "10.112.0.0/12"

    # kubernetes
    # Track the minor and let GKE resolve the patch, so a retired build can't break
    # cluster-create. A full build was previously pinned to dodge a Cilium
    # endpoint-deletion race (affected 1.33/1.34/1.35) -- re-pin here and on the
    # gke.py node pools if that recurs.
    kubernetes_version: str = "1.34"
    node_pools: list[NodePool] | None = None

    # dns
    parent_dns_zone_name: str = "byoc.pinecone.io"

    # features
    public_access_enabled: bool = True
    deletion_protection: bool = True
    # Set to a NexusConfig to deploy Nexus alongside the DB stack. None = DB-only.
    nexus: NexusConfig | None = None

    # pinecone specific
    api_url: str = "https://api.pinecone.io"
    global_env: str = "prod"
    auth0_domain: str = "https://login.pinecone.io"
    # Base URL of the Pinecone web console (workspace deep links). Override
    # for preprod/internal installs.
    console_url: str = "https://app.pinecone.io"
    data_plane_backend: str = "postgres"  # "postgres" | "fdb"

    # cross-cloud: AWS account for AMP federation
    amp_aws_account_id: str = "713131977538"

    # tags/labels
    labels: dict[str, str] | None = None

    # workload identity - K8s service accounts that need GCS access
    writer_k8s_service_accounts: list[str] | None = None
    reader_k8s_service_accounts: list[str] | None = None

    def __post_init__(self):
        require_external_fdb_for_nexus(self.nexus, self.data_plane_backend)
        # With fewer than 3 zones the FoundationDB CR silently degrades from zone
        # to hostname fault domains, and nothing downstream validates it.
        if self.data_plane_backend == "fdb" and len(self.availability_zones) < 3:
            raise ValueError(
                "data_plane_backend='fdb' requires at least 3 availability zones "
                f"for zone fault domains, got {self.availability_zones!r}."
            )


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
                is_nexus_enabled=args.nexus is not None,
            ),
            opts=child_opts,
        )

        self._cell_name = _cell_name(self._environment)
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

        # fdb cells run on FoundationDB and need no AlloyDB; only the postgres backend provisions it.
        self._alloydb = (
            AlloyDB(
                f"{config.resource_prefix}-alloydb",
                config,
                self._vpc.network_id,
                self._vpc.private_ip_range_name,
                self._vpc.private_connection,
                self._cell_name,
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vpc]),
            )
            if args.data_plane_backend == "postgres"
            else None
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
            workspace_routing_enabled=args.nexus is not None,
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
            control_db=self._alloydb.control_db if self._alloydb is not None else None,
            system_db=self._alloydb.system_db if self._alloydb is not None else None,
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
            "data_plane_backend": args.data_plane_backend,
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
            # Enable the netstack *.wksp route only when Nexus is wired -- the same
            # condition that turns on gateway.workspaceAuth (common/nexus.py), so
            # routing and its auth edge go live in lockstep and never on a
            # non-Nexus BYOC cell (which has no nexus-gateway to route to).
            "workspace_routing_enabled": args.nexus is not None,
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
            data_plane_backend=args.data_plane_backend,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=list(filter(None, [self._gke, self._dns, self._gcs, self._alloydb])),
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
            config_map_dependencies=self._k8s_configmaps.config_maps,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke, self._k8s_configmaps]),
        )

        # Install Nexus after the DB stack is ready.
        self._nexus = None
        self._nexus_gcs = None
        self._nexus_project_id = None
        self._default_workspace = None
        self._default_workspace_name = DEFAULT_WORKSPACE_NAME
        self.__default_workspace_exists = None
        if args.nexus is not None:
            nx = args.nexus
            # Per-cell default name so a torn-down cell's leftover workspace can't
            # block a re-deploy; explicit config wins.
            self._default_workspace_name = default_workspace_name(
                nx.default_workspace_name, self._resource_suffix
            )
            # Nexus versions independently of the DB stack (separate repo, separate
            # image tags), so there is no meaningful fallback to pinecone_version --
            # a DB tag never names a nexus_deploy/nexus_* image. Require it explicitly
            # rather than producing an unpullable image ref.
            if nx.version is None:
                raise ValueError(
                    "nexus.version must be set to the Nexus image tag (the nexus "
                    "images.yml build tag). It is unrelated to the DB pinecone_version."
                )
            # Durable GCS storage is always provisioned for GCP+Nexus (the `fs`
            # default isn't durable and file upload needs object storage). The
            # bucket prefix is derived from the cell name (`pc-nexus-{cell}`),
            # mirroring the vault-id derivation -- the cell name is minted
            # server-side mid-deploy, so the operator can't supply it in advance.
            # `storage_bucket_prefix` is an optional override, not a gate.
            storage_prefix = nx.storage_bucket_prefix or self._cell_name.apply(
                lambda cn: f"pc-nexus-{cn}"
            )
            self._nexus_gcs = NexusGCSBuckets(
                f"{config.resource_prefix}-nexus-gcs",
                config,
                cell_name=self._cell_name,
                prefix=storage_prefix,
                force_destroy=not args.deletion_protection,
                # The WI binding inside references the cluster's Workload Identity
                # pool ({project}.svc.id.goog), which only exists once a GKE cluster
                # with WI has been created. Depend on the GKE component so a
                # fresh-project first deploy doesn't race the binding ahead of the
                # pool (Error 400: Identity Pool does not exist).
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gke]),
            )
            blob_storage = NexusBlobStorage(
                source=self._nexus_gcs.source,
                knowledge=self._nexus_gcs.knowledge,
                archive=self._nexus_gcs.archive,
            )
            # Annotate the Nexus KSAs so the pods assume the GCS SA via WI.
            nexus_sa_annotations = self._nexus_gcs.gcs_sa_email.apply(
                lambda email: {"iam.gke.io/gcp-service-account": email}
            )
            self._nexus_project_id = require_byoc_project_id(nx)
            self._nexus = Nexus(
                f"{config.resource_prefix}-nexus",
                k8s_provider=self._gke.k8s_provider,
                image_registry=(nx.image_registry or NEXUS_GCP_REGISTRY.base_url),
                nexus_version=nx.version,
                byoc_env=nx.byoc_env or self._environment.env_name,
                cloud="gcp",
                region=args.region,
                pinecone_prod=args.global_env == "prod",
                byoc_project_id=self._nexus_project_id,
                byoc_vault_id=(
                    nx.byoc_vault_id or self._resource_suffix.apply(lambda s: f"byoc{s}")
                ),
                blob_storage=blob_storage,
                service_account_annotations=nexus_sa_annotations,
                # CPGW index client: Nexus reaches the control-plane gateway at
                # {api_url}/internal/cpgw (synchronous CPS db_index_id on create).
                # Paired with the cpgw-api-key in the nexus-config secret.
                cpgw_api_url=f"{args.api_url}/internal/cpgw",
                inference_models_toml=nx.inference_models_toml,
                fdb_mode=nx.fdb_mode,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[
                        r
                        for r in [
                            self._gke,
                            self._k8s_secrets,
                            self._k8s_configmaps,
                            self._gcr_refresher,
                            self._pinetools,
                            self._nlb,
                            self._nexus_gcs,
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
                kubeconfig=self._gke.kubeconfig,
                deploy_image=self._nexus.deploy_image,
                cloud="gcp",
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._nexus]),
            )

            # Depends on Nexus because the cell's operation poller is what
            # promotes the workspace to Ready — creating before it exists would
            # wait on nothing. First-run-only (no-op diff/delete): destroy
            # leaves the workspace; delete it via gCPS before teardown.
            self._default_workspace = DefaultWorkspace(
                f"{config.resource_prefix}-default-workspace",
                DefaultWorkspaceArgs(
                    name=self._default_workspace_name,
                    environment=nx.byoc_env or self._environment.env_name,
                    api_url=args.api_url,
                    pinecone_api_key=args.pinecone_api_key,
                ),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._nexus]),
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
                "control_db_endpoint": self._alloydb.control_db.endpoint if self._alloydb else None,
                "system_db_endpoint": self._alloydb.system_db.endpoint if self._alloydb else None,
                "environment_id": self._environment.id,
                "environment_name": self._environment.env_name,
                "service_account_id": self._service_account.id,
                "service_account_client_id": self._service_account.client_id,
                "api_key_project_id": self._api_key.project_id,
                "subdomain": self._subdomain,
                "sli_checkers_project_id": self._api_key.project_id,
                "cpgw_api_key": self._k8s_secrets.cpgw_api_key,
                "cpgw_admin_api_key_id": self._cpgw_api_key.key_id,
                "datadog_api_key_id": (
                    self._datadog_api_key.key_id if self._datadog_api_key is not None else None
                ),
                "customer_tags": config.custom_tags,
                "pulumi_backend_url": self._pulumi_operator.backend_url,
                "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
                "psc_service_attachment": self._nlb.service_attachment.self_link,
            }
        )

    def _build_config(self, args: PineconeGCPClusterArgs):
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

        # Add Nexus node pools when enabled; DB-only deploys are unaffected.
        if args.nexus is not None:
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
            nexus_enabled=args.nexus is not None,
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
    def alloydb(self) -> AlloyDB | None:
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
                    self._default_workspace_name,
                ).apply(lambda a: api.workspace_exists(a[0], a[1], a[3]))
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
            self._default_workspace_name,
        )
        return pulumi.Output.all(self._default_workspace_exists(), url).apply(
            lambda a: a[1] if a[0] else None
        )
