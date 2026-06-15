"""GCS buckets for Nexus blob storage (source, knowledge, archive)."""

import pulumi
import pulumi_gcp as gcp

from config.gcp import GCPConfig

_NEXUS_BUCKETS = ("source", "knowledge", "archive")


class NexusGCSBuckets(pulumi.ComponentResource):
    """Three GCS buckets backing the Nexus blob storage backend.

    Provisioned when ``NexusConfig.storage_bucket_prefix`` is set. Bucket names
    follow the pattern ``{prefix}-{suffix}`` where suffix is one of
    ``source``, ``knowledge``, ``archive``. Pass the outputs to
    ``NexusBlobStorage`` to wire them into the Nexus helm release.
    """

    def __init__(
        self,
        name: str,
        config: GCPConfig,
        prefix: str,
        force_destroy: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:NexusGCSBuckets", name, None, opts)

        child_opts = pulumi.ResourceOptions(parent=self)

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

        self.register_outputs(
            {s: self._buckets[s].name for s in _NEXUS_BUCKETS}
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
