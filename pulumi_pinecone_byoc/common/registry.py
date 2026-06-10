"""Container registry configuration per cloud provider."""

from dataclasses import dataclass
from typing import Literal

DEFAULT_PINETOOLS_TAG = "latest"

RegistryType = Literal["ecr", "gcr", "acr"]


@dataclass(frozen=True)
class ContainerRegistry:
    base_url: str
    type: RegistryType

    def pinetools_image(self, tag: str = DEFAULT_PINETOOLS_TAG) -> str:
        return f"{self.base_url}/pinetools:{tag}"


AWS_REGISTRY = ContainerRegistry(
    base_url="843333058014.dkr.ecr.us-east-1.amazonaws.com/unstable/pinecone/v4",
    type="ecr",
)

GCP_REGISTRY = ContainerRegistry(
    base_url="us-docker.pkg.dev/pinecone-artifacts/unstable",
    type="gcr",
)

# Nexus images live in their own Artifact Registry repo (`nexus`), co-located on
# the DB registry host so the BYOC pull secret (`regcred`, keyed by host) covers
# both. DB/pinetools images stay in the `unstable` repo on the same host.
NEXUS_GCP_REGISTRY = ContainerRegistry(
    base_url="us-docker.pkg.dev/pinecone-artifacts/nexus",
    type="gcr",
)

AZURE_REGISTRY = ContainerRegistry(
    base_url="pinecone.azurecr.io/unstable/pinecone/v4",
    type="acr",
)

# Nexus images on Azure live in their own `nexus` repo, co-located on the ACR
# host so the BYOC pull secret (`regcred`, keyed by host) covers both. Mirrors
# NEXUS_GCP_REGISTRY. DB/pinetools images stay in the `unstable` repo.
NEXUS_AZURE_REGISTRY = ContainerRegistry(
    base_url="pinecone.azurecr.io/nexus",
    type="acr",
)
