"""Azure Blob containers for Nexus blob storage (source, knowledge, archive)."""

import pulumi
import pulumi_azure_native as azure_native

_NEXUS_CONTAINERS = ("source", "knowledge", "archive")


class NexusBlobContainers(pulumi.ComponentResource):
    """Three blob containers backing the Nexus blob storage backend.

    Provisioned when ``NexusConfig.storage_bucket_prefix`` is set. Containers
    are created inside the existing DB storage account so the same access key
    covers both the DB and Nexus blob data. Container names follow the pattern
    ``{prefix}-{suffix}`` where suffix is one of ``source``, ``knowledge``,
    ``archive``. Pass the outputs to ``NexusBlobStorage`` to wire them into
    the Nexus helm release.
    """

    def __init__(
        self,
        name: str,
        prefix: str,
        storage_account_name: pulumi.Input[str],
        resource_group_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:NexusBlobContainers", name, None, opts)

        child_opts = pulumi.ResourceOptions(parent=self)

        self._containers: dict[str, azure_native.storage.BlobContainer] = {}
        for suffix in _NEXUS_CONTAINERS:
            self._containers[suffix] = azure_native.storage.BlobContainer(
                f"{name}-{suffix}",
                account_name=storage_account_name,
                container_name=f"{prefix}-{suffix}",
                resource_group_name=resource_group_name,
                opts=child_opts,
            )

        self.register_outputs({s: self._containers[s].name for s in _NEXUS_CONTAINERS})

    @property
    def source(self) -> pulumi.Output[str]:
        return self._containers["source"].name

    @property
    def knowledge(self) -> pulumi.Output[str]:
        return self._containers["knowledge"].name

    @property
    def archive(self) -> pulumi.Output[str]:
        return self._containers["archive"].name
