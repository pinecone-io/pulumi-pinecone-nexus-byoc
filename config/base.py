"""Base configuration for BYOC infrastructure (cloud-agnostic)."""

from pydantic import BaseModel, Field


class NodePoolTaint(BaseModel):
    key: str
    value: str
    effect: str = "NO_SCHEDULE"


class NodePoolConfig(BaseModel):
    """Shared node pool config. Each cloud reads its own field: instance_type (AWS), machine_type (GCP), vm_size (Azure)."""

    name: str
    # AWS-only
    instance_type: str = "r6in.large"
    desired_size: int = 3
    disk_type: str = "gp3"
    # GCP-only
    machine_type: str = "n2-standard-4"
    fixed_node_count_per_zone: int | None = None
    node_locations: list[str] | None = None
    min_cpu_platform: str | None = "Intel Ice Lake"
    auto_repair: bool = False
    # Azure-only
    vm_size: str = "Standard_D4s_v5"
    # Common
    min_size: int = 1
    max_size: int = 10
    disk_size_gb: int = 100
    labels: dict[str, str] = Field(default_factory=dict)
    taints: list[NodePoolTaint] = Field(default_factory=list)


class BaseConfig(BaseModel):
    region: str
    global_env: str = "prod"
    cloud: str = "aws"

    availability_zones: list[str]
    vpc_cidr: str = "10.0.0.0/16"
    kubernetes_version: str = "1.33"
    node_pools: list[NodePoolConfig] = Field(default_factory=list)
    parent_zone_name: str = "pinecone.io"

    # Mint a real Datadog API key via CPGW. When false, the datadog-api-key secret
    # still gets a placeholder so the DB platform's envoy-stats-relay starts (no
    # stats ship) -- use on dev cells where minting 400s on the org's Datadog quota.
    # TODO(temporary): a workaround for the platform hard-requiring the secret; the
    # real fix is platform-side (tolerate a missing key) or reusing a shared key.
    datadog_enabled: bool = True

    @property
    def resource_prefix(self) -> str:
        return "pc"
