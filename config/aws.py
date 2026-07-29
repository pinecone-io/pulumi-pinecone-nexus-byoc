"""
Configuration for Pinecone BYOC AWS infrastructure.
"""

from pydantic import BaseModel, Field

from .base import BaseConfig

APN_TAG_PROD_BYOC = ("aws-apn-id", "pc:5eldspisdx06ewzohqetnxufm")
APN_TAG_NONPROD_BYOC = ("aws-test-apn-id", "pc:00000000000000000000-byoc")


class DatabaseInstanceConfig(BaseModel):
    """Configuration for a single RDS instance."""

    name: str
    db_name: str
    username: str
    instance_class: str = "db.r8g.large"
    engine_version: str = "15.15"
    deletion_protection: bool = False
    backup_retention_days: int = 7


class DatabaseConfig(BaseModel):
    """RDS database configuration with control-db and system-db instances."""

    engine_version: str = "15.15"
    deletion_protection: bool = False
    backup_retention_days: int = 7

    # Control database (1 shard)
    control_db: DatabaseInstanceConfig = DatabaseInstanceConfig(
        name="control-db",
        db_name="controller",
        username="controller",
        instance_class="db.r8g.large",
    )

    # System database
    system_db: DatabaseInstanceConfig = DatabaseInstanceConfig(
        name="system-db",
        db_name="systemdb",
        username="systemuser",
        instance_class="db.r8g.large",
    )


class AWSConfig(BaseConfig):
    """
    AWS-specific configuration for BYOC infrastructure.

    Extends BaseConfig with AWS-specific settings.
    """

    cloud: str = "aws"

    # Networking. When unset, subnet masks are derived from the VPC prefix:
    # public = vpc_prefix + 4, private = vpc_prefix + 2 (so a /20 VPC yields
    # /24 public and /22 private subnets, matching the historical /16 -> /20//18
    # layout). Set explicitly only to override the derived sizes.
    public_subnet_mask: int | None = None
    private_subnet_mask: int | None = None

    # Database
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    # Custom AMI
    custom_ami_id: str | None = None

    # KMS key ARN for encrypting S3 and RDS
    kms_key_arn: str | None = None

    # Custom tags from user
    custom_tags: dict[str, str] = Field(default_factory=dict)

    def tags(self, **extra: str) -> dict[str, str]:
        """Generate consistent resource tags, including user-provided custom tags."""
        apn_key, apn_value = (
            APN_TAG_PROD_BYOC if self.global_env == "prod" else APN_TAG_NONPROD_BYOC
        )
        base_tags = {
            "pinecone:managed-by": "pulumi",
            apn_key: apn_value,
        }
        return {**base_tags, **self.custom_tags, **extra}
