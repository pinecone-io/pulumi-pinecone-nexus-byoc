"""Nexus deployment component.

Installs the vendored Nexus Helm releases into the BYOC cluster after the
Pinecone DB stack is up. Two releases are installed in order:

  1. ``nexus-fdb`` — FoundationDB for Nexus. The app release references its
     resources by name, so it must come first.
  2. ``nexus`` — the app services (api, orchestrator, knowql, file-proxy,
     console, gateway). Depends on the fdb release and the DB stack bootstrap.

Charts are the vendored copies at ``<repo>/nexus/deploy/helm/{nexus,nexus-fdb}``.
"""

import hashlib
import tomllib
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

# (namespace, KSA name) for the chart's KSAs that touch blob storage; cloud
# Workload-Identity wiring binds/annotates exactly these. nexus-api backs both
# api and file-proxy; nexus-task does the actual object reads/writes.
NEXUS_KSA_MEMBERS: tuple[tuple[str, str], ...] = (
    (_NEXUS_NAMESPACE, "nexus-api"),
    (_NEXUS_NAMESPACE, "nexus-orchestrator"),
    (_NEXUS_TASKS_NAMESPACE, "nexus-task"),
)

_DEFAULT_STORAGE_CLASS = "premium-rwo"
# GKE uses gce-internal; AKS passes None (gateway exposed directly via LoadBalancer).
_DEFAULT_INGRESS_CLASS = "gce-internal"

# Public FoundationDB image. The nexus-fdb chart defaults to the nexus-alpha AR
# mirror, which a BYOC node service account cannot read without a cross-project
# grant; the upstream public image is identical and needs no extra IAM.
_FDB_IMAGE_REPOSITORY = "foundationdb/foundationdb"

# In-cluster svc-docs-api base for the keyless BYOC data path (#548): the DB
# platform is co-located, so task pods reach its docs-api over cluster-internal DNS.
# TODO: these coords belong to the DB platform (separate repo) -- confirm the svc
# name / namespace / port there, and that they don't differ by cloud.
_DEFAULT_DOCS_API_URL = "http://docs-api.pc-docs-api.svc.cluster.local:3001"

# Inference-proxy routing overlay. The customer's model config is layered onto
# the proxy's baked default.toml as the `byoc` cascade profile. These names are
# the contract with the Nexus chart (deploy/helm/nexus/values.yaml,
# templates/services/inference-proxy.yaml): the ConfigMap holds a `byoc.toml`
# key, and `byoc` is appended to the chart's default configProfiles.
_INFERENCE_BYOC_CONFIGMAP = "nexus-inference-proxy-byoc-config"
_INFERENCE_BYOC_PROFILE = "byoc"
# Base configProfiles for a BYOC inference overlay. Empty so the byoc profile
# stands alone: byoc.toml resets the catalog/tiers, but layering the chart's
# "development" base re-introduces its claude/nebius tier refs (which the reset
# does not clear), tripping the proxy's startup assert when only a gemini key
# is present.
_CHART_BASE_CONFIG_PROFILE = ""
# Surfaces whose model entries carry an api_key_ref to project as a pod env var.
_PROVIDER_KEY_SURFACES = ("llm_models", "embedding_models", "rerank_models")

# Clean-slate sentinel prepended to the customer's routing TOML. It makes the
# proxy drop its baked routing table (catalog + profiles, incl. the dev/prod
# claude tier overrides) before this layer applies, so the customer's config is
# authoritative rather than a deep-merge onto the shipped default. Injected here
# so the operator-facing TOML stays purely about models -- it never has to know
# about the cascade. See nexus-inference-proxy settings (_ResettableRoutingTomlSource).
_RESET_SENTINEL_HEADER = (
    "# Managed by Pinecone BYOC: start from a clean routing table (drop the\n"
    "# proxy's built-in model catalog/tiers) before applying the config below.\n"
    "reset_inference_proxy_config = true\n\n"
)


def derive_api_key_refs(inference_models_toml: str) -> list[str]:
    """Distinct ``api_key_ref`` values across the overlay's model catalog.

    Pinecone-style models carry no ``api_key_ref`` (the caller supplies the key
    per request via the ``Api-Key`` header), so they're skipped. The sorted
    result drives both the ``nexus-config`` Secret keys the deploy provisions
    and the chart's ``inference-proxy.providerKeyRefs`` projection list, so the
    two never drift -- both come from this one parse of the TOML.
    """
    parsed = tomllib.loads(inference_models_toml)
    refs: set[str] = set()
    for surface in _PROVIDER_KEY_SURFACES:
        for model in parsed.get(surface, {}).values():
            if not isinstance(model, dict) or model.get("api_style") == "pinecone":
                continue
            ref = model.get("api_key_ref")
            if isinstance(ref, str) and ref:
                refs.add(ref)
    return sorted(refs)

@dataclass
class NexusConfig:
    """Nexus enablement settings. Pass to cluster args to deploy Nexus alongside the DB stack.

    On GCP, durable object storage is always provisioned: the cluster creates
    three buckets (``{prefix}-source/-knowledge/-archive``) plus the GCS SA and
    Workload Identity wiring, and switches Nexus to the blob backend. The prefix
    is derived from the cell name (``pc-nexus-{cell}``) -- minted server-side
    mid-deploy, so the operator can't supply it in advance. Set
    ``storage_bucket_prefix`` only to override that derived prefix.

    On Azure ``storage_bucket_prefix`` is opt-in: unset keeps the ``fs``
    backend; set provisions blob containers.
    """

    version: str | None = None  # falls back to pinecone_version
    byoc_env: pulumi.Input[str] | None = None  # falls back to minted env name
    image_registry: str | None = None  # falls back to cloud-specific default
    gemini_api_key: pulumi.Input[str] | None = None
    inference_base: pulumi.Input[str] | None = None  # falls back to api_url
    # BYOC single-tenant project id. None => the project the deploy mints for the
    # cell (the __SLI__ ApiKey's project_id, also exported as sli_checkers_project_id);
    # set only to pin Nexus to a different, pre-existing project.
    byoc_project_id: pulumi.Input[str] | None = None
    # Short DNS-safe vault id; forms the index host's leftmost label
    # `nexus-{context_id}-{vault}`, which must stay <= 63 chars. None => a derived
    # `byoc{cell-suffix}` slug.
    # TODO(temporary): every non-Nexus caller sends the project's *real* vault_id
    # from the project record; Nexus has no project store yet, so it can't look it
    # up and uses this slug (cpgw trusts the value, so it works). Drop the slug once
    # Nexus can resolve the real vault -- via its own/DB auth service, or by cpgw
    # deriving vault_id from project_info.id.
    byoc_vault_id: pulumi.Input[str] | None = None
    # In-cluster svc-docs-api base URL for the keyless BYOC data path (#548).
    # None => the co-located DB default (_DEFAULT_DOCS_API_URL).
    byoc_docs_api_url: pulumi.Input[str] | None = None
    # Override for the storage bucket prefix. GCP: None => derived `pc-nexus-{cell}`
    # (storage always provisioned); set to override. Azure: None => fs backend, set => blob.
    storage_bucket_prefix: str | None = None
    # Inference-proxy model routing. When set, the proxy loads this TOML as the
    # `byoc` config overlay (model catalog + the default profile's tiers) on top
    # of its baked default. ``provider_keys`` maps each ``api_key_ref`` in the
    # TOML to its secret value (e.g. {"gemini-api-key": <secret>}); the wizard
    # collects it via ``pulumi config --secret nexus-provider-keys.<ref>``.
    inference_models_toml: str | None = None
    provider_keys: pulumi.Input[dict] | None = None


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
        byoc_project_id: pulumi.Input[str],
        byoc_vault_id: pulumi.Input[str] | None = None,
        storage_class: str = _DEFAULT_STORAGE_CLASS,
        ingress_class: str | None = _DEFAULT_INGRESS_CLASS,
        blob_storage: "NexusBlobStorage | None" = None,
        service_account_annotations: pulumi.Input[dict] | None = None,
        cpgw_api_url: pulumi.Input[str] | None = None,
        byoc_docs_api_url: pulumi.Input[str] | None = None,
        inference_models_toml: str | None = None,
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
            byoc_project_id: BYOC single-tenant project id (the cell's minted
                ``__SLI__`` project id).
            byoc_vault_id: Short DNS-safe vault id used as the index host label
                ``nexus-{context_id}-{vault}``. Keeps that leftmost label <= 63 chars
                (a full UUID overflows it). Sent to CPGW as ``project_info.vault_id``.
            storage_class: StorageClass for Nexus PVCs. Defaults to GKE ``premium-rwo``.
            ingress_class: Gateway Ingress class annotation. Pass ``None`` to omit (AKS).
            blob_storage: Provisioned blob bucket/container names. When set, switches
                the storage backend to ``blob`` and passes the names into the helm chart.
                Leave ``None`` to use the local filesystem backend (``fs``).
            service_account_annotations: Annotations applied to the chart's KSAs
                via ``serviceAccountAnnotations``. On GKE this carries
                ``iam.gke.io/gcp-service-account=<sa-email>`` so the pods assume
                the blob-storage SA via Workload Identity.
            cpgw_api_url: CPGW control-plane gateway base URL (``…/internal/cpgw``).
                When set, Nexus uses the CPGW index client (synchronous CPS
                ``db_index_id`` on create) instead of the managed public path.
                Must be paired with the ``cpgw-api-key`` entry in the
                ``nexus-config`` secret — setting one without the other makes
                Nexus panic at startup (partial CPGW config).
            byoc_docs_api_url: In-cluster svc-docs-api base URL for the keyless BYOC
                data path (#548). Defaults to the co-located DB's docs-api. Only
                applied when ``cpgw_api_url`` is set.
            inference_models_toml: BYOC inference-proxy routing overlay. When set, a
                ConfigMap holding it as ``byoc.toml`` is provisioned and the chart is
                pointed at it (``byoc`` appended to configProfiles); leave ``None`` to
                run the proxy on its baked default routing table.
        """
        super().__init__("pinecone:byoc:Nexus", name, None, opts)

        self._image_registry = image_registry
        self._byoc_project_id = byoc_project_id
        self._byoc_vault_id = byoc_vault_id

        provider_opts = pulumi.ResourceOptions(parent=self, provider=k8s_provider)

        fdb_values: dict = {
            "image": {
                "pullSecrets": [{"name": _REGCRED}],
            },
            "foundationdb": {
                "image": {
                    "repository": _FDB_IMAGE_REPOSITORY,
                },
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

        # KSA annotations for Workload Identity (GKE: gcp-service-account=<email>).
        if service_account_annotations is not None:
            app_values["serviceAccountAnnotations"] = service_account_annotations

        # Set explicitly: Nexus otherwise defaults the vault id to byocProjectId,
        # whose full UUID overflows the 63-char DNS label of the index host.
        if byoc_vault_id is not None:
            app_values["config"]["byocVaultId"] = byoc_vault_id

        # CPGW index client. When the cluster can reach the control-plane gateway,
        # point Nexus at its ``…/internal/cpgw`` base so index create/delete use the
        # CPGW path (synchronous CPS db_index_id) instead of the managed public API.
        # The chart gates on truthiness; the paired ``cpgw-api-key`` is delivered via
        # the nexus-config secret (set together or Nexus panics on partial config).
        # The keyless data path needs svc-docs-api too, injected as byocDocsApiUrl.
        if cpgw_api_url is not None:
            app_values["config"]["cpgwApiUrl"] = cpgw_api_url
            app_values["config"]["byocDocsApiUrl"] = (
                byoc_docs_api_url or _DEFAULT_DOCS_API_URL
            )

        # BYOC inference-proxy routing overlay. Ship the customer's model config
        # as a ConfigMap mounted as the `byoc` cascade profile. The TOML sets
        # reset_inference_proxy_config = true, so the proxy drops the baked
        # routing table (catalog + profiles) before this layer applies -- the
        # customer's config is authoritative, not a deep-merge onto the shipped
        # default (which also clears the dev/prod claude tier overrides). The
        # configChecksum (a hash of the TOML) rolls the proxy pod when the
        # overlay changes -- the subPath mount doesn't live-update.
        # providerKeyRefs is derived from the same TOML so the projected env
        # vars match the catalog's api_key_refs.
        app_release_depends_on = [self.fdb_release]
        if inference_models_toml is not None:
            # Prepend the clean-slate sentinel here so the operator-facing TOML
            # never carries cascade plumbing. derive_api_key_refs ignores the
            # bool; the checksum hashes the final content so edits roll the pod.
            byoc_toml = _RESET_SENTINEL_HEADER + inference_models_toml
            self.inference_config = k8s.core.v1.ConfigMap(
                f"{name}-inference-proxy-byoc-config",
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=_INFERENCE_BYOC_CONFIGMAP,
                    namespace=_NEXUS_NAMESPACE,
                ),
                data={"byoc.toml": byoc_toml},
                opts=provider_opts,
            )
            app_release_depends_on.append(self.inference_config)
            checksum = hashlib.sha256(byoc_toml.encode("utf-8")).hexdigest()
            # Append byoc as the last (highest-precedence) profile, idempotently:
            # keep whatever base profiles are already selected and only add byoc
            # if absent. Nothing sets configProfiles upstream today, so this falls
            # back to the chart's default base.
            existing = app_values.get("configProfiles", _CHART_BASE_CONFIG_PROFILE)
            profiles = [p.strip() for p in existing.split(",") if p.strip()]
            if _INFERENCE_BYOC_PROFILE not in profiles:
                profiles.append(_INFERENCE_BYOC_PROFILE)
            app_values["configProfiles"] = ",".join(profiles)
            app_values["inference-proxy"] = {
                "byocConfigMap": _INFERENCE_BYOC_CONFIGMAP,
                "configChecksum": f"sha256-{checksum[:16]}",
                "providerKeyRefs": derive_api_key_refs(inference_models_toml),
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
                depends_on=app_release_depends_on,
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
        # TODO(nexus-prod): HTTP-only, no TLS. The gateway is the customer front
        # door — needs a real TLS/ingress story (cert + https) before prod.
        annotations: dict = {"kubernetes.io/ingress.allow-http": "true"}
        if ingress_class is not None:
            annotations["kubernetes.io/ingress.class"] = ingress_class
        else:
            # AKS: no controller serves this class-less Ingress (the gateway is
            # exposed via the cluster LB / Gloo gateway-proxy), so its
            # .status.loadBalancer is never populated. Skip Pulumi's readiness
            # await so `pulumi up` doesn't hang waiting for an LB address.
            # TODO(nexus-prod): replace skipAwait with a real readiness model
            # (await the gateway LB / a proper Ingress controller).
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
