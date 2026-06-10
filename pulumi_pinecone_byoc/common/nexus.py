"""Nexus deployment component (proposal §4.1 / §4.4 / §4.5, tasks 2.4-2.6).

Installs the vendored Nexus Helm releases into the BYOC cluster *after* the
Pinecone DB stack is up. Parallel in spirit to `Pinetools`: a single
`pulumi.ComponentResource` that owns the cluster-side install and is wired into
`gcp/cluster.py` gated on `nexus_enabled` so DB-only deploys are unaffected.

Two releases are installed, in order:
  1. `nexus-fdb` — FoundationDB for Nexus (`b"nx"` keyspace). The app release
     references its resources by name (`nexus-fdb-cluster` ConfigMap,
     `nexus-fdb-headless` Service), so it must come first.
  2. `nexus` — the app services (api, orchestrator, knowql, file-proxy,
     console, gateway). depends_on the fdb release *and* the DB stack so the
     in-cluster data plane / control-plane bootstrap (Pinetools) is ready.

Charts are the vendored copies at `<repo>/nexus/deploy/helm/{nexus,nexus-fdb}`.
"""

from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.helm.v3 import Release, ReleaseArgs

# Repo-root-relative path to the vendored Nexus charts. This module lives at
# `<repo>/pulumi_pinecone_byoc/common/nexus.py`, so the repo root is two
# package levels up from the package dir.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEXUS_CHART = str(_REPO_ROOT / "nexus" / "deploy" / "helm" / "nexus")
_NEXUS_FDB_CHART = str(_REPO_ROOT / "nexus" / "deploy" / "helm" / "nexus-fdb")

# Namespaces the secrets/cred-refresher already provision for Nexus
# (common/k8s_secrets.py mints `nexus-pinecone-api-key` here; cred_refresher.py
# materializes `regcred` in both `nexus` and `nexus-tasks`).
_NEXUS_NAMESPACE = "nexus"
_NEXUS_TASKS_NAMESPACE = "nexus-tasks"

# regcred image pull secret name, brokered into the nexus namespaces by
# RegistryCredentialRefresher (task 2.3).
_REGCRED = "regcred"

# The k8s Secret created in `nexus` by K8sSecrets when nexus_enabled. The
# Nexus chart's BYOC surface (task 1.7) references it for PINECONE_API_KEY
# (and INFERENCE_API_KEY, which defaults to the same deployment key for the
# PoC — proposal §10).
_PINECONE_KEY_SECRET = "nexus-pinecone-api-key"
_PINECONE_KEY_SECRET_KEY = "PINECONE_API_KEY"

# StorageClass the PoC uses for Nexus PVCs (FDB data + tasks/source/knowledge/
# contexts). The DB side does not provision a custom StorageClass (it relies on
# the GKE default), so the PoC reuses GKE's built-in dynamic SSD class. RWX
# (Filestore/NFS) is intentionally left unconfigured for the PoC — see the
# module-level note and proposal §11. Override via `storage_class` if a cluster
# default differs (Azure/AKS passes `managed-csi`).
_DEFAULT_STORAGE_CLASS = "premium-rwo"

# Ingress class for the Nexus gateway Ingress. On GKE the DB stack uses the
# internal GCE ingress controller, so the Nexus front door rides the same
# `gce-internal` class. On AKS there is no equivalent ingress-class annotation
# (gcp/nlb.py vs azure/nlb.py: the Azure path exposes the gloo gateway directly
# via LoadBalancer Services with no `kubernetes.io/ingress.class`), so the Azure
# caller passes `ingress_class=None` to omit the annotation entirely. Defaults to
# the GCP value so GCP behavior is unchanged.
_DEFAULT_INGRESS_CLASS = "gce-internal"


class Nexus(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        k8s_provider: pulumi.ProviderResource,
        image_registry: str,
        nexus_version: pulumi.Input[str],
        pinecone_api_base: pulumi.Input[str],
        byoc_env: pulumi.Input[str],
        inference_base: pulumi.Input[str] | None = None,
        storage_class: str = _DEFAULT_STORAGE_CLASS,
        ingress_class: str | None = _DEFAULT_INGRESS_CLASS,
        nfs_server: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Install the Nexus stack into the BYOC cluster.

        Args:
            image_registry: BYOC container registry base URL (common/registry.py
                GCP_REGISTRY.base_url). Nexus images live under
                `<registry>/nexus_<component>:<nexus_version>` (chart appends the
                per-component repository + tag).
            nexus_version: image tag for the Nexus images (proposal §10
                `nexus-version`; coordinated with task 2.7).
            pinecone_api_base: managed control-plane base (PINECONE_API_BASE,
                e.g. https://api.pinecone.io). Index CRUD stays managed.
            byoc_env: the `.byoc` deployment environment id (PINECONE_BYOC_ENV /
                chart `deployment.environment`). Vector ops auto-follow the
                in-cluster host the control plane returns for this env.
            inference_base: managed inference base (INFERENCE_BASE). Defaults to
                `pinecone_api_base` (PoC: inference is the same managed endpoint).
            storage_class: StorageClass for Nexus PVCs (task 2.5). Defaults to the
                GKE `premium-rwo` class; AKS passes `managed-csi`.
            ingress_class: value for the gateway Ingress
                `kubernetes.io/ingress.class` annotation. Defaults to
                `gce-internal` (GKE). Pass `None` to omit the annotation entirely
                (AKS, where the gloo gateway is exposed directly with no ingress
                class).
            nfs_server: optional pre-provisioned Filestore/NFS server IP for RWX
                (proposal §11 infra ask). Left unset for the PoC.
        """
        super().__init__("pinecone:byoc:Nexus", name, None, opts)

        self._image_registry = image_registry

        provider_opts = pulumi.ResourceOptions(parent=self, provider=k8s_provider)

        if inference_base is None:
            inference_base = pinecone_api_base

        # ------------------------------------------------------------------
        # 1. nexus-fdb release (must precede the app release; the app chart
        #    references nexus-fdb-cluster / nexus-fdb-headless by name).
        # ------------------------------------------------------------------
        fdb_values: dict = {
            "image": {
                "pullSecrets": [{"name": _REGCRED}],
            },
            # FDB lands on the services pool (mirrors the app chart scheduling).
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

        # ------------------------------------------------------------------
        # 2. nexus app release. depends_on the fdb release; the DB-stack
        #    ordering is enforced by the caller (gcp/cluster.py passes the
        #    Pinetools install as a dependency via opts.depends_on).
        # ------------------------------------------------------------------
        persistence: dict = {
            "storageClass": storage_class,
        }
        if nfs_server is not None:
            # RWX path: chart mounts a pre-provisioned Filestore share. Left
            # unset for the PoC (RWO per-component PVCs) unless a server is
            # provisioned out-of-band (proposal §11).
            persistence["nfs"] = {"server": nfs_server}

        app_values: dict = {
            # dev/prod expect externally managed secrets (Pulumi); this is not
            # a local-runtime install.
            "env": "prod",
            "localRuntime": False,
            "image": {
                "registry": image_registry,
                "tag": nexus_version,
                "pullSecrets": [{"name": _REGCRED}],
            },
            # Task pods are launched into nexus-tasks (chart default); regcred is
            # refreshed there too (task 2.3).
            "orchestrator": {
                "taskNamespace": _NEXUS_TASKS_NAMESPACE,
            },
            # Storage (task 2.5).
            "persistence": persistence,
            # BYOC config surface (proposal §4.1 / §10, chart task 1.7). Index
            # CRUD + inference stay on the managed control plane; vector ops
            # auto-follow the in-cluster host the control plane returns. These
            # keys match the vendored chart's `pineconeByoc` values block
            # (nexus/deploy/helm/nexus/values.yaml), which the
            # `nexus.byocEnvSettings` helper threads into the api/orchestrator/
            # knowql + task-pod env (PINECONE_BYOC / PINECONE_API_BASE /
            # PINECONE_BYOC_ENV / INFERENCE_BASE, plus PINECONE_API_KEY /
            # INFERENCE_API_KEY sourced from the external secret below).
            "pineconeByoc": {
                # PINECONE_BYOC master switch — enable the BYOC env block.
                "enabled": True,
                # PINECONE_API_BASE — managed control plane (index CRUD).
                "apiBase": pinecone_api_base,
                # PINECONE_BYOC_ENV — the `.byoc` deployment.environment id.
                "byocEnv": byoc_env,
                # INFERENCE_BASE — embed/rerank endpoint (managed for the PoC).
                "inferenceBase": inference_base,
                # localRuntime-only keys: left empty because this is a
                # dev/prod (env=prod) install that reads the key from the
                # external secret below, not from chart values.
                "apiKey": "",
                "inferenceApiKey": "",
                # External Secret holding the minted deployment key. The
                # Nexus component (task 2.2) mints it into `nexus-pinecone-api-key`
                # with key PINECONE_API_KEY in the nexus namespace. The chart
                # sources both PINECONE_API_KEY and INFERENCE_API_KEY from it;
                # an empty inferenceApiKeyKey reuses apiKeyKey, so the inference
                # key defaults to the deployment key for the PoC (§10).
                "secret": {
                    "name": _PINECONE_KEY_SECRET,
                    "apiKeyKey": _PINECONE_KEY_SECRET_KEY,
                    "inferenceApiKeyKey": "",
                },
            },
            # Ingress (task 2.6): the Nexus gateway is the customer front door
            # exposed through the EXISTING ingress/LB (gcp/nlb.py routes
            # *.pinecone.io -> the `gateway-proxy` Service in gloo-system). The
            # Nexus gateway attaches to that same LB rather than allocating its
            # own cloud LB, so it stays a ClusterIP and the DB data plane is
            # never exposed. See `attach_gateway_to_lb` for the routing glue.
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

        # ------------------------------------------------------------------
        # 3. Ingress glue (task 2.6). Route the existing gloo ingress at a
        #    nexus host to the Nexus gateway Service. The DB data plane keeps
        #    its own routing (gateway-proxy in gloo-system) untouched and
        #    internal-only; we only ADD a path for the Nexus front door.
        # ------------------------------------------------------------------
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
            }
        )

    def _attach_gateway_to_lb(
        self,
        name: str,
        k8s_provider: pulumi.ProviderResource,
        ingress_class: str | None = _DEFAULT_INGRESS_CLASS,
    ) -> k8s.networking.v1.Ingress:
        """Expose the Nexus gateway through the existing customer LB.

        The existing internal/external LB (gcp/nlb.py, azure/nlb.py) terminates
        TLS for the cluster wildcard hosts and points at the `gateway-proxy`
        Service in gloo-system. The Nexus gateway runs as a ClusterIP Service in
        the `nexus` namespace; this Ingress object adds a route so customer
        traffic to the Nexus host lands on the Nexus gateway. The DB data plane
        routing is left as-is and stays internal-only.

        On GKE the route attaches to the internal GCE ingress controller via the
        `gce-internal` ingress class. On AKS there is no equivalent class
        annotation (the gloo gateway is exposed directly by LoadBalancer
        Services), so `ingress_class=None` omits the annotation entirely.

        Kept minimal for the PoC: a single Ingress in the nexus namespace
        backed by the nexus gateway ClusterIP Service on port 80.
        """
        annotations = {
            # HTTP is enabled for the PoC: no TLS cert is wired up here, and the
            # GCE ingress controller refuses to provision an LB when both HTTP
            # and HTTPS are disabled.
            "kubernetes.io/ingress.allow-http": "true",
        }
        if ingress_class is not None:
            # Attach to the same internal ingress controller the DB stack uses
            # (GKE: gce-internal); the customer front door rides the existing LB
            # rather than provisioning a new one. Omitted on AKS.
            annotations["kubernetes.io/ingress.class"] = ingress_class

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
