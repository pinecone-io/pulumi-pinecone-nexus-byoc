"""GCS buckets for Nexus blob storage (source, knowledge, archive).

Also provisions the Workload Identity wiring the Nexus pods need to reach those
buckets: a dedicated GCS service account granted ``roles/storage.objectAdmin``
on the three buckets, bound to the Nexus Kubernetes service accounts via
``roles/iam.workloadIdentityUser``. The SA email is exposed so the caller can
annotate the chart's KSAs (``iam.gke.io/gcp-service-account``) -- on a
Workload-Identity cluster an un-annotated KSA has no GCP identity and does not
fall back to the node SA, so without this the pods get no GCS access.
"""

import pulumi
import pulumi_gcp as gcp

from config.gcp import GCPConfig

from ..common.nexus import NEXUS_KSA_MEMBERS
from .gke import _sa_id

_NEXUS_BUCKETS = ("source", "knowledge", "archive")


class NexusGCSBuckets(pulumi.ComponentResource):
    """Three GCS buckets backing the Nexus blob storage backend, plus the
    Workload Identity service account the Nexus pods use to access them.

    Provisioned when ``NexusConfig.storage_bucket_prefix`` is set. Bucket names
    follow the pattern ``{prefix}-{suffix}`` where suffix is one of
    ``source``, ``knowledge``, ``archive``. Pass the outputs to
    ``NexusBlobStorage`` to wire the bucket names into the Nexus helm release,
    and pass ``gcs_sa_email`` to ``Nexus`` so the chart annotates its KSAs for
    Workload Identity.
    """

    def __init__(
        self,
        name: str,
        config: GCPConfig,
        cell_name: pulumi.Input[str],
        prefix: str,
        force_destroy: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:NexusGCSBuckets", name, None, opts)

        child_opts = pulumi.ResourceOptions(parent=self)
        cell = pulumi.Output.from_input(cell_name)

        self._buckets: dict[str, gcp.storage.Bucket] = {}
        for suffix in _NEXUS_BUCKETS:
            self._buckets[suffix] = gcp.storage.Bucket(
                f"{name}-{suffix}",
                name=f"{prefix}-{suffix}",
                project=config.project,
                location=config.region,
                force_destroy=force_destroy,
                uniform_bucket_level_access=True,
                versioning=gcp.storage.BucketVersioningArgs(enabled=True),
                lifecycle_rules=[
                    gcp.storage.BucketLifecycleRuleArgs(
                        action=gcp.storage.BucketLifecycleRuleActionArgs(
                            type="AbortIncompleteMultipartUpload",
                        ),
                        condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=1),
                    ),
                    gcp.storage.BucketLifecycleRuleArgs(
                        action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
                        condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                            days_since_noncurrent_time=7,
                        ),
                    ),
                ],
                labels=config.labels(),
                opts=child_opts,
            )

        # Dedicated GCS identity for the Nexus pods. Mirrors the DB data-pod
        # pattern (a purpose-scoped SA bound to the KSAs via Workload Identity)
        # but scoped to just the three Nexus buckets rather than project-wide
        # storage. read/write of context source/knowledge/archive objects ->
        # objectAdmin (read, create, delete).
        self._gcs_sa = gcp.serviceaccount.Account(
            f"{name}-sa",
            account_id=cell.apply(lambda cn: _sa_id("nexus-gcs", cn)),
            display_name=cell.apply(lambda cn: f"Nexus GCS service account for {cn}"),
            opts=child_opts,
        )

        for suffix in _NEXUS_BUCKETS:
            gcp.storage.BucketIAMMember(
                f"{name}-{suffix}-objadmin",
                bucket=self._buckets[suffix].name,
                role="roles/storage.objectAdmin",
                member=self._gcs_sa.email.apply(lambda e: f"serviceAccount:{e}"),
                opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gcs_sa]),
            )

        # Bind each Nexus KSA to the GCS SA. The members resolve to
        # serviceAccount:<project>.svc.id.goog[<ns>/<ksa>]; the chart annotates
        # those same KSAs with iam.gke.io/gcp-service-account=<gcs-sa-email>.
        gcp.serviceaccount.IAMBinding(
            f"{name}-sa-workload-identity",
            service_account_id=self._gcs_sa.name,
            role="roles/iam.workloadIdentityUser",
            members=pulumi.Output.all(config.project).apply(
                lambda args: [
                    f"serviceAccount:{args[0]}.svc.id.goog[{ns}/{ksa}]"
                    for ns, ksa in NEXUS_KSA_MEMBERS
                ]
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._gcs_sa]),
        )

        self.register_outputs(
            {
                **{s: self._buckets[s].name for s in _NEXUS_BUCKETS},
                "gcs_sa_email": self._gcs_sa.email,
            }
        )

    @property
    def source(self) -> pulumi.Output[str]:
        return self._buckets["source"].name

    @property
    def knowledge(self) -> pulumi.Output[str]:
        return self._buckets["knowledge"].name

    @property
    def archive(self) -> pulumi.Output[str]:
        return self._buckets["archive"].name

    @property
    def gcs_sa_email(self) -> pulumi.Output[str]:
        """Email of the GCS SA the Nexus KSAs impersonate via Workload Identity.

        Annotate the chart's KSAs with
        ``iam.gke.io/gcp-service-account=<this>`` (via ``serviceAccountAnnotations``).
        """
        return self._gcs_sa.email
