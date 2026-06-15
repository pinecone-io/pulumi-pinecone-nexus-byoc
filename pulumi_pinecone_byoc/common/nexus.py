"""Nexus deployment component.

Installs the vendored Nexus Helm releases into the BYOC cluster after the
Pinecone DB stack is up. Two releases are installed in order:

  1. ``nexus-fdb`` — FoundationDB for Nexus. The app release references its
     resources by name, so it must come first.
  2. ``nexus`` — the app services (api, orchestrator, knowql, file-proxy,
     console, gateway). Depends on the fdb release and the DB stack bootstrap.

Charts are the vendored copies at ``<repo>/nexus/deploy/helm/{nexus,nexus-fdb}``.
"""

from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.helm.v3 import Release, ReleaseArgs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEXUS_CHART = str(_REPO_ROOT / "nexus" / "deploy" / "helm" / "nexus")
_NEXUS_FDB_CHART = str(_REPO_ROOT / "nexus" / "deploy" / "helm" / "nexus-fdb")

_NEXUS_NAMESPACE = "nexus"
_NEXUS_TASKS_NAMESPACE = "nexus-tasks"
_REGCRED = "regcred"

_DEFAULT_BYOC_PROJECT_ID = "byoc-poc"
_DEFAULT_STORAGE_CLASS = "premium-rwo"
# GKE uses gce-internal; AKS passes None (gateway exposed directly via LoadBalancer).
_DEFAULT_INGRESS_CLASS = "gce-internal"

@dataclass
class NexusConfig:
    """Nexus enablement settings. Pass to cluster args to deploy Nexus alongside the DB stack.

    Storage defaults to the local filesystem backend (``fs``). Set
    ``storage_bucket_prefix`` to a string prefix and the cluster will provision
    three buckets/containers (``{prefix}-source``, ``{prefix}-knowledge``,
    ``{prefix}-archive``) and switch Nexus to the blob backend.
    """

    version: str | None = None  # falls back to pinecone_version
    byoc_env: pulumi.Input[str] | None = None  # falls back to minted env name
    image_registry: str | None = None  # falls back to cloud-specific default
    gemini_api_key: pulumi.Input[str] | None = None
    inference_base: pulumi.Input[str] | None = None  # falls back to api_url
    byoc_project_id: pulumi.Input[str] = _DEFAULT_BYOC_PROJECT_ID
    storage_bucket_prefix: str | None = None  # None = fs backend, set = provision blob


class Nexus(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        k8s_provider: pulumi.ProviderResource,
        image_registry: str,
        nexus_version: pulumi.Input[str],
        byoc_env: pulumi.Input[str],
        cloud: pulumi.Input[str],
        region: pulumi.Input[str],
        pinecone_prod: bool,
        byoc_project_id: pulumi.Input[str] = _DEFAULT_BYOC_PROJECT_ID,
        storage_class: str = _DEFAULT_STORAGE_CLASS,
        ingress_class: str | None = _DEFAULT_INGRESS_CLASS,
        blob_storage: "NexusBlobStorage | None" = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Install the Nexus stack into the BYOC cluster.

        Args:
            image_registry: Container registry base URL for Nexus images.
            nexus_version: Image tag for Nexus images.
            byoc_env: The ``.byoc`` deployment environment id.
            cloud: Cloud provider for index placement (``"azure"``/``"gcp"``).
            region: Deploy region for index placement.
            pinecone_prod: False on preprod omits the preprod header.
            byoc_project_id: BYOC single-tenant project id.
            storage_class: StorageClass for Nexus PVCs. Defaults to GKE ``premium-rwo``.
            ingress_class: Gateway Ingress class annotation. Pass ``None`` to omit (AKS).
            blob_storage: Provisioned blob bucket/container names. When set, switches
                the storage backend to ``blob`` and passes the names into the helm chart.
                Leave ``None`` to use the local filesystem backend (``fs``).
        """
        super().__init__("pinecone:byoc:Nexus", name, None, opts)

        self._image_registry = image_registry
        self._byoc_project_id = byoc_project_id

        provider_opts = pulumi.ResourceOptions(parent=self, provider=k8s_provider)

        fdb_values: dict = {
            "image": {
                "pullSecrets": [{"name": _REGCRED}],
            },
            "persistence": {
                "storageClass": storage_class,
            },
        }

        self.fdb_release = Release(
            f"{name}-fdb",
            ReleaseArgs(
                name="nexus-fdb",
                chart=_NEXUS_FDB_CHART,
                namespace=_NEXUS_NAMESPACE,
                values=fdb_values,
            ),
            opts=provider_opts,
        )

        if blob_storage is not None:
            storage_cfg: dict = {
                "backend": "blob",
                "source": blob_storage.source,
                "knowledge": blob_storage.knowledge,
                "archive": blob_storage.archive,
            }
            if blob_storage.account_name is not None:
                # Chart reads config.storage.azure.account (nested), not .account.
                storage_cfg["azure"] = {"account": blob_storage.account_name}
        else:
            storage_cfg = {"backend": "fs"}

        app_values: dict = {
            "env": "prod",
            "localRuntime": False,
            "image": {
                "registry": image_registry,
                "tag": nexus_version,
                "pullSecrets": [{"name": _REGCRED}],
            },
            "orchestrator": {
                "taskNamespace": _NEXUS_TASKS_NAMESPACE,
            },
            "persistence": {
                "storageClass": storage_class,
            },
            "config": {
                "deploymentMode": "byoc",
                "environment": byoc_env,
                "cloud": {
                    "provider": cloud,
                    "region": region,
                },
                "pineconeProd": pinecone_prod,
                "byocProjectId": byoc_project_id,
                "storage": storage_cfg,
            },
            # Gateway runs as ClusterIP; exposed via the existing ingress/LB.
            "gateway": {
                "service": {
                    "type": "ClusterIP",
                    "port": 80,
                },
            },
        }

        self.app_release = Release(
            f"{name}-app",
            ReleaseArgs(
                name="nexus",
                chart=_NEXUS_CHART,
                namespace=_NEXUS_NAMESPACE,
                values=app_values,
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[self.fdb_release],
            ),
        )

        self.gateway_ingress = self._attach_gateway_to_lb(
            name,
            k8s_provider,
            ingress_class,
        )

        self.register_outputs(
            {
                "namespace": _NEXUS_NAMESPACE,
                "fdb_release": self.fdb_release.name,
                "app_release": self.app_release.name,
                "byoc_project_id": self._byoc_project_id,
            }
        )

    def _attach_gateway_to_lb(
        self,
        name: str,
        k8s_provider: pulumi.ProviderResource,
        ingress_class: str | None = _DEFAULT_INGRESS_CLASS,
    ) -> k8s.networking.v1.Ingress:
        """Expose the Nexus gateway through the existing cluster LB.

        On GKE attaches via the ``gce-internal`` ingress class. On AKS
        ``ingress_class=None`` omits the annotation (gateway exposed directly
        via LoadBalancer Services).
        """
        annotations: dict = {"kubernetes.io/ingress.allow-http": "true"}
        if ingress_class is not None:
            annotations["kubernetes.io/ingress.class"] = ingress_class
        else:
            # AKS: no controller serves this class-less Ingress (the gateway is
            # exposed via the cluster LB / Gloo gateway-proxy), so its
            # .status.loadBalancer is never populated. Skip Pulumi's readiness
            # await so `pulumi up` doesn't hang waiting for an LB address.
            annotations["pulumi.com/skipAwait"] = "true"

        return k8s.networking.v1.Ingress(
            f"{name}-gateway-ingress",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="nexus-gateway",
                namespace=_NEXUS_NAMESPACE,
                annotations=annotations,
            ),
            spec=k8s.networking.v1.IngressSpecArgs(
                default_backend=k8s.networking.v1.IngressBackendArgs(
                    service=k8s.networking.v1.IngressServiceBackendArgs(
                        name="nexus-gateway",
                        port=k8s.networking.v1.ServiceBackendPortArgs(number=80),
                    ),
                ),
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[self.app_release],
            ),
        )

    @property
    def image_registry(self) -> str:
        return self._image_registry

    @property
    def byoc_project_id(self) -> pulumi.Input[str]:
        return self._byoc_project_id


@dataclass
class NexusBlobStorage:
    """Provisioned blob storage bucket/container names for Nexus.

    Set all three to switch the Nexus storage backend to ``blob``.
    Produced by cloud-specific provisioning (``NexusGCSBuckets`` on GCP,
    ``NexusBlobContainers`` on Azure) and passed into the ``Nexus`` component.
    """

    source: pulumi.Input[str]
    knowledge: pulumi.Input[str]
    archive: pulumi.Input[str]
    # Azure storage account name. Set on Azure (chart emits AZURE_STORAGE_ACCOUNT);
    # GCS leaves None.
    account_name: pulumi.Input[str] | None = None
