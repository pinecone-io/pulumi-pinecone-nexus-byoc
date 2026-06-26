"""PineconeAzureCluster - main component for BYOC deployments on Azure."""

from dataclasses import dataclass, field

import pulumi
import pulumi_azure_native as azure_native
import pulumi_azuread as azuread

from ..common.cred_refresher import RegistryCredentialRefresher
from ..common.k8s_configmaps import K8sConfigMaps
from ..common.k8s_secrets import K8sSecrets, NexusSecretConfig
from ..common.naming import cell_name as _cell_name
from ..common.nexus import Nexus, NexusBlobStorage, NexusConfig, derive_api_key_refs
from ..common.pinetools import Pinetools
from ..common.providers import (
    AmpAccess,
    AmpAccessArgs,
    ApiKey,
    ApiKeyArgs,
    CpgwApiKey,
    CpgwApiKeyArgs,
    DATADOG_DISABLED_PLACEHOLDER,
    DatadogApiKey,
    DatadogApiKeyArgs,
    Environment,
    EnvironmentArgs,
    ServiceAccount,
    ServiceAccountArgs,
)
from ..common.registry import AZURE_REGISTRY, NEXUS_AZURE_REGISTRY
from ..common.uninstaller import ClusterUninstaller
from .aks import AKS
from .database import Database
from .dns import DNS
from .k8s_addons import K8sAddons
from .nlb import InternalLoadBalancer
from .pulumi_operator import PulumiOperator
from .nexus_storage import NexusBlobContainers
from .storage import BlobStorage
from .vnet import VNet


@dataclass
class NodePool:
    name: str
    vm_size: str = "Standard_D4s_v7"
    min_size: int = 1
    max_size: int = 10
    disk_size_gb: int = 100
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)


@dataclass
class PineconeAzureClusterArgs:
    # required
    pinecone_api_key: pulumi.Input[str]
    pinecone_version: str

    # azure specific
    subscription_id: str = ""
    region: str = "eastus"
    availability_zones: list[str] = field(default_factory=lambda: ["1", "2"])

    # networking
    vpc_cidr: str = "10.0.0.0/16"

    # kubernetes
    kubernetes_version: str = "1.33"
    node_pools: list[NodePool] | None = None

    # dns
    parent_dns_zone_name: str = "byoc.pinecone.io"

    # features
    public_access_enabled: bool = True
    deletion_protection: bool = True
    # when True, provision the Azure AD Application + ServicePrincipal (and the
    # subscription-scoped Storage Blob Data Reader role) that the data-importer
    # uses for cross-account blob reads. Requires the deploying identity to hold
    # the Entra directory permission to create a ServicePrincipal. Defaults to
    # False; DB + Nexus ingest->query does not need it.
    storage_integration_enabled: bool = False
    # Set to a NexusConfig to deploy Nexus alongside the DB stack. None = DB-only.
    nexus: NexusConfig | None = None

    # pinecone specific
    api_url: str = "https://api.pinecone.io"
    global_env: str = "prod"
    auth0_domain: str = "https://login.pinecone.io"

    # cross-cloud: AWS account for AMP federation
    amp_aws_account_id: str = "713131977538"

    # cross-cloud: gcp project for helmfile templates
    gcp_project: str = "production-pinecone"

    # tags
    tags: dict[str, str] | None = None


class PineconeAzureCluster(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        args: PineconeAzureClusterArgs,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:PineconeAzureCluster", name, None, opts)

        self.args = args
        if not args.subscription_id:
            raise ValueError("subscription_id is required for Azure deployments")
        child_opts = pulumi.ResourceOptions(parent=self)
        config = self._build_config(args)
        self._config = config

        client_config = azure_native.authorization.get_client_config()
        tenant_id = client_config.tenant_id

        self._environment = Environment(
            f"{config.resource_prefix}-environment",
            EnvironmentArgs(
                cloud="azure",
                region=args.region,
                global_env=args.global_env,
                api_url=args.api_url,
                secret=args.pinecone_api_key,
                is_public_endpoint_enabled=args.public_access_enabled,
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
                opts=pulumi.ResourceOptions(
                    parent=self, depends_on=[self._cpgw_api_key]
                ),
            )
        else:
            self._datadog_api_key = None

        self._vnet = VNet(
            f"{config.resource_prefix}-vnet",
            config,
            self._cell_name,
            opts=child_opts,
        )

        self._aks = AKS(
            f"{config.resource_prefix}-aks",
            config,
            resource_group_name=self._vnet.resource_group_name,
            subnet_id=self._vnet.aks_subnet_id,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vnet]),
        )

        self._storage = BlobStorage(
            f"{config.resource_prefix}-storage",
            config,
            cell_name=self._cell_name,
            resource_group_name=self._vnet.resource_group_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._aks]),
        )

        self._database = Database(
            f"{config.resource_prefix}-database",
            config,
            resource_group_name=self._vnet.resource_group_name,
            vnet_id=self._vnet.vnet_id,
            delegated_subnet_id=self._vnet.db_subnet_id,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._vnet]),
        )

        self._subdomain = self._environment.env_name

        self._dns = DNS(
            f"{config.resource_prefix}-dns",
            subdomain=self._subdomain.apply(lambda name: name.removesuffix(".byoc")),
            parent_zone_name=args.parent_dns_zone_name,
            api_url=args.api_url,
            cpgw_api_key=self._cpgw_api_key.key,
            cell_name=self._cell_name,
            resource_group_name=self._vnet.resource_group_name,
            location=config.region,
            tags=config.tags(),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._cpgw_api_key]),
        )

        self._k8s_addons = K8sAddons(
            f"{config.resource_prefix}-k8s-addons",
            config,
            k8s_provider=self._aks.k8s_provider,
            oidc_issuer_url=self._aks.oidc_issuer_url,
            resource_group_name=self._vnet.resource_group_name,
            dns_zone_id=self._dns.zone.id,
            cell_name=self._cell_name,
            tenant_id=tenant_id,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._aks]),
        )

        self._nlb = InternalLoadBalancer(
            f"{config.resource_prefix}-nlb",
            config=config,
            k8s_provider=self._aks.k8s_provider,
            resource_group_name=self._vnet.resource_group_name,
            pls_subnet_name=self._vnet.pls_subnet_name,
            dns_zone_name=self._dns.zone.name,
            subdomain=self._dns.subdomain,
            external_ip_address=self._dns.external_ip.ip_address.apply(lambda ip: ip or ""),
            cell_name=self._cell_name,
            public_access_enabled=args.public_access_enabled,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self._vnet, self._dns, self._aks, self._k8s_addons],
            ),
        )

        # Storage integration: Azure AD app for data-importer blob access.
        # Gated on storage_integration_enabled (default False) — requires Entra
        # directory permission the deploying identity may lack.
        storage_integration_app_client_id: pulumi.Input[str] | None = None
        storage_integration_password_value: pulumi.Input[str] | None = None
        if args.storage_integration_enabled:
            storage_integration_app = azuread.Application(
                f"{config.resource_prefix}-storage-integration-app",
                display_name=self._cell_name.apply(lambda cn: f"{cn}-storage-integration"),
                opts=child_opts,
            )
            storage_integration_sp = azuread.ServicePrincipal(
                f"{config.resource_prefix}-storage-integration-sp",
                client_id=storage_integration_app.client_id,
                opts=child_opts,
            )
            storage_integration_password = azuread.ServicePrincipalPassword(
                f"{config.resource_prefix}-storage-integration-password",
                service_principal_id=storage_integration_sp.id,
                opts=child_opts,
            )
            # Storage Blob Data Reader at subscription scope so the data-importer
            # can read from any storage account the customer points their import URI at.
            STORAGE_BLOB_DATA_READER_ROLE = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
            azure_native.authorization.RoleAssignment(
                f"{config.resource_prefix}-storage-integration-role",
                principal_id=storage_integration_sp.object_id,
                principal_type="ServicePrincipal",
                role_definition_id=pulumi.Output.from_input(config.subscription_id).apply(
                    lambda sid: (
                        f"/subscriptions/{sid}/providers/Microsoft.Authorization"
                        f"/roleDefinitions/{STORAGE_BLOB_DATA_READER_ROLE}"
                    )
                ),
                scope=pulumi.Output.from_input(config.subscription_id).apply(
                    lambda sid: f"/subscriptions/{sid}"
                ),
                opts=child_opts,
            )
            storage_integration_app_client_id = storage_integration_app.client_id
            storage_integration_password_value = storage_integration_password.value

        self._k8s_secrets = K8sSecrets(
            f"{config.resource_prefix}-k8s-secrets",
            k8s_provider=self._aks.k8s_provider,
            cpgw_api_key=self._cpgw_api_key.key,
            gcps_api_key=self._api_key.value,
            dd_api_key=(
                self._datadog_api_key.api_key
                if self._datadog_api_key is not None
                else DATADOG_DISABLED_PLACEHOLDER
            ),
            nexus=NexusSecretConfig(
                api_key=args.pinecone_api_key,
                gemini_api_key=args.nexus.gemini_api_key,
                azure_storage_access_key=(
                    self._storage.access_key
                    if args.nexus.storage_bucket_prefix is not None
                    else None
                ),
                provider_keys=args.nexus.provider_keys,
                provider_key_refs=(
                    derive_api_key_refs(args.nexus.inference_models_toml)
                    if args.nexus.inference_models_toml is not None
                    else None
                ),
            ) if args.nexus is not None else None,
            control_db=self._database.control_db,
            system_db=self._database.system_db,
            azure_storage_access_key=self._storage.access_key,
            storage_integration_credentials=(
                {"client-secret": storage_integration_password_value}
                if storage_integration_password_value is not None
                else None
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    r
                    for r in [
                        self._aks,
                        self._cpgw_api_key,
                        self._api_key,
                        self._datadog_api_key,
                        self._database,
                    ]
                    if r is not None
                ],
            ),
        )

        self._pulumi_operator = PulumiOperator(
            f"{config.resource_prefix}-pulumi-operator",
            config,
            resource_group_name=self._vnet.resource_group_name,
            resource_group_id=self._vnet.resource_group_id,
            storage_account=self._storage.storage_account,
            oidc_issuer_url=self._aks.oidc_issuer_url,
            tenant_id=tenant_id,
            cell_name=self._cell_name,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._aks, self._storage]),
        )

        # AMP: CPGW acts as credential broker for Azure (no direct OIDC federation)
        # the per-customer role trusts the CPGW IAM user, which assumes it via STS
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
            "cloud": "azure",
            "region": config.region,
            "global_env": config.global_env,
            "subdomain": self._subdomain,
            "availability_zones": config.availability_zones,
            "dns_zone_name": self._dns.zone.name,
            "azure_k8s_version": args.kubernetes_version,
            "azure_subscription_id": config.subscription_id,
            "azure_subnet_id": self._vnet.aks_subnet_id,
            "azure_client_id": self._k8s_addons.dns_identity_client_id,
            "azure_certmanager_client_id": self._k8s_addons.certmanager_identity_client_id,
            "azure_tenant_id": tenant_id,
            "azure_resource_group": self._vnet.resource_group.name,
            "azure_pulumi_operator_client_id": self._pulumi_operator.identity_client_id,
            "data_storage_account_name": self._storage.account_name,
            "image_registry": AZURE_REGISTRY.base_url,
            "sli_checkers_project_id": self._api_key.project_id,
            "gcp_project": args.gcp_project,
            "cpgw_admin_api_key_id": self._cpgw_api_key.key_id,
            "api_url": args.api_url,
            "auth0_domain": args.auth0_domain,
            "customer_tags": args.tags or {},
            "public_access_enabled": args.public_access_enabled,
            "pulumi_backend_url": self._pulumi_operator.backend_url,
            "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
            "aws_amp_region": self._amp_access.amp_region,
            "aws_amp_remote_write_url": self._amp_access.amp_remote_write_endpoint,
            "aws_amp_sigv4_role_arn": self._amp_access.pinecone_role_arn,
            "aws_amp_ingest_role_arn": "",
            # None when storage integration is disabled; the configmap component
            # omits None-valued entries.
            "azure_storage_integration_tenant_id": (
                tenant_id if storage_integration_app_client_id is not None else None
            ),
            "azure_storage_integration_client_id": storage_integration_app_client_id,
        }

        self._k8s_configmaps = K8sConfigMaps(
            f"{config.resource_prefix}-k8s-configmaps",
            k8s_provider=self._aks.k8s_provider,
            cloud="azure",
            cell_name=self._cell_name,
            env=config.global_env,
            is_prod=config.global_env == "prod",
            domain=self._subdomain,
            region=config.region,
            public_access_enabled=args.public_access_enabled,
            pulumi_outputs=pulumi_outputs,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self._aks, self._dns, self._storage, self._database],
            ),
        )

        self._acr_refresher = RegistryCredentialRefresher(
            f"{config.resource_prefix}-acr-refresher",
            k8s_provider=self._aks.k8s_provider,
            cpgw_url=args.api_url,
            registry=AZURE_REGISTRY.type,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._k8s_secrets]),
        )

        self._pinetools = Pinetools(
            f"{config.resource_prefix}-pinetools",
            k8s_provider=self._aks.k8s_provider,
            pinecone_version=args.pinecone_version,
            pinetools_image=AZURE_REGISTRY.pinetools_image(args.pinecone_version),
            config_map_dependencies=self._k8s_configmaps.config_maps,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._aks, self._k8s_configmaps]),
        )

        # Install Nexus after the DB stack is ready.
        self._nexus = None
        self._nexus_containers = None
        if args.nexus is not None:
            nx = args.nexus
            blob_storage = None
            if nx.storage_bucket_prefix is not None:
                self._nexus_containers = NexusBlobContainers(
                    f"{config.resource_prefix}-nexus-containers",
                    prefix=nx.storage_bucket_prefix,
                    storage_account_name=self._storage.storage_account.name,
                    resource_group_name=self._vnet.resource_group_name,
                    opts=pulumi.ResourceOptions(parent=self, depends_on=[self._storage]),
                )
                blob_storage = NexusBlobStorage(
                    source=self._nexus_containers.source,
                    knowledge=self._nexus_containers.knowledge,
                    archive=self._nexus_containers.archive,
                    account_name=self._storage.account_name,
                )
            self._nexus = Nexus(
                f"{config.resource_prefix}-nexus",
                k8s_provider=self._aks.k8s_provider,
                image_registry=(nx.image_registry or NEXUS_AZURE_REGISTRY.base_url),
                nexus_version=nx.version or args.pinecone_version,
                byoc_env=nx.byoc_env or self._environment.env_name,
                cloud="azure",
                region=args.region,
                pinecone_prod=args.global_env == "prod",
                byoc_project_id=nx.byoc_project_id or self._api_key.project_id,
                byoc_vault_id=(
                    nx.byoc_vault_id
                    or self._resource_suffix.apply(lambda s: f"byoc{s}")
                ),
                storage_class="managed-csi",
                ingress_class=None,
                blob_storage=blob_storage,
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
                            self._aks,
                            self._k8s_secrets,
                            self._k8s_configmaps,
                            self._acr_refresher,
                            self._pinetools,
                            self._nlb,
                            self._nexus_containers,
                        ]
                        if r is not None
                    ],
                ),
            )

        self._uninstaller = ClusterUninstaller(
            f"{config.resource_prefix}-uninstaller",
            kubeconfig=self._aks.kubeconfig,
            pinetools_image=AZURE_REGISTRY.pinetools_image(args.pinecone_version),
            cloud="azure",
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[
                    self._pinetools.ns,
                    self._pinetools.sa,
                    self._pinetools.crb,
                    self._k8s_addons,
                    self._k8s_secrets,
                    self._k8s_configmaps,
                    self._acr_refresher,
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
                "vnet_id": self._vnet.vnet_id,
                "kubeconfig": self._aks.kubeconfig,
                "storage_account_name": self._storage.account_name,
                "control_db_endpoint": self._database.control_db.endpoint,
                "system_db_endpoint": self._database.system_db.endpoint,
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
                    self._datadog_api_key.key_id
                    if self._datadog_api_key is not None
                    else None
                ),
                "customer_tags": config.custom_tags,
                "pulumi_backend_url": self._pulumi_operator.backend_url,
                "pulumi_secrets_provider": self._pulumi_operator.secrets_provider,
                "private_link_service_name": self._nlb.pls_name,
                "private_link_service_resource_group": self._vnet.resource_group_name.apply(
                    lambda rg: f"{rg}-nodepool"
                ),
            }
        )

    def _build_config(self, args: PineconeAzureClusterArgs):
        from config.azure import AzureConfig, FlexibleServerConfig
        from config.base import NodePoolConfig

        node_pools = []
        if args.node_pools:
            for np in args.node_pools:
                node_pools.append(
                    NodePoolConfig(
                        name=np.name,
                        vm_size=np.vm_size,
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
                    vm_size="Standard_D4s_v7",
                    min_size=1,
                    max_size=10,
                    disk_size_gb=100,
                ),
            ]

        # Add Nexus node pools when enabled; DB-only deploys are unaffected.
        if args.nexus is not None:
            from .aks import nexus_node_pools

            node_pools.extend(nexus_node_pools())

        return AzureConfig(
            region=args.region,
            global_env=args.global_env,
            cloud="azure",
            subscription_id=args.subscription_id,
            availability_zones=args.availability_zones,
            vpc_cidr=args.vpc_cidr,
            kubernetes_version=args.kubernetes_version,
            node_pools=node_pools,
            parent_zone_name=args.parent_dns_zone_name,
            database=FlexibleServerConfig(
                deletion_protection=args.deletion_protection,
            ),
            custom_tags=args.tags or {},
        )

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def name(self) -> pulumi.Output[str]:
        return self._aks.cluster.name

    @property
    def vnet(self) -> VNet:
        return self._vnet

    @property
    def aks(self) -> AKS:
        return self._aks

    @property
    def storage(self) -> BlobStorage:
        return self._storage

    @property
    def database(self) -> Database:
        return self._database

    @property
    def dns(self) -> DNS:
        return self._dns

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

    @property
    def private_link_service_name(self) -> pulumi.Output[str]:
        return self._nlb.pls_name

    @property
    def private_link_service_resource_group(self) -> pulumi.Output[str]:
        return self._vnet.resource_group_name.apply(lambda rg: f"{rg}-nodepool")
