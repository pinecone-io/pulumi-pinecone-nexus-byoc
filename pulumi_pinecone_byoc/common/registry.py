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

# Nexus images live in a separate Artifact Registry repo (nexus-alpha), NOT the
# DB `unstable` repo. The Nexus component pulls `nexus_<component>` images from
# here while the DB/pinetools images stay on GCP_REGISTRY (unstable).
NEXUS_GCP_REGISTRY = ContainerRegistry(
    base_url="us-east1-docker.pkg.dev/pinecone-artifacts/nexus-alpha",
    type="gcr",
)

AZURE_REGISTRY = ContainerRegistry(
    base_url="pinecone.azurecr.io/unstable/pinecone/v4",
    type="acr",
)
