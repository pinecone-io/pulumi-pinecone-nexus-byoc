"""S3 buckets for Nexus blob storage, plus the IRSA wiring the Nexus pods need
to reach them (a dedicated IAM role assumed by the Nexus KSAs via the cluster's
OIDC provider). AWS mirror of gcp/nexus_gcs.py:NexusGCSBuckets."""

import json

import pulumi
import pulumi_aws as aws

from config.aws import AWSConfig

from ..common.nexus import NEXUS_KSA_MEMBERS

_NEXUS_BUCKETS = ("source", "knowledge", "archive")


class NexusS3Buckets(pulumi.ComponentResource):
    """Three S3 buckets (``{prefix}-source/knowledge/archive``) plus the IAM
    role the Nexus pods assume via IRSA to access them.

    Always provisioned for AWS+Nexus. ``prefix`` is a derived ``pc-nexus-{cell}``
    Output unless ``NexusConfig.storage_bucket_prefix`` overrides it. Pass the
    bucket outputs to ``NexusBlobStorage`` and ``role_arn`` to the ``Nexus``
    component's ``service_account_annotations``
    (``eks.amazonaws.com/role-arn``) so the EKS pod-identity webhook injects
    web-identity credentials — pc_blob's S3 backend picks them up through the
    default AWS credential chain.
    """

    def __init__(
        self,
        name: str,
        config: AWSConfig,
        cell_name: pulumi.Input[str],
        prefix: pulumi.Input[str],
        oidc_provider_arn: pulumi.Input[str],
        oidc_provider_url: pulumi.Input[str],
        kms_key_arn: pulumi.Input[str] | None = None,
        force_destroy: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:NexusS3Buckets", name, None, opts)

        child_opts = pulumi.ResourceOptions(parent=self)
        cell = pulumi.Output.from_input(cell_name)
        resource_suffix = cell.apply(lambda cn: cn[-4:])
        # `prefix` may be a derived Output (e.g. `pc-nexus-{cell}`) or a literal
        # override, so resolve it through Output before building bucket names.
        prefix_out = pulumi.Output.from_input(prefix)

        self._buckets: dict[str, aws.s3.Bucket] = {}
        for suffix in _NEXUS_BUCKETS:
            bucket_name = prefix_out.apply(lambda p, s=suffix: f"{p}-{s}")
            bucket = aws.s3.Bucket(
                f"{name}-{suffix}",
                bucket=bucket_name,
                force_destroy=force_destroy,
                tags=bucket_name.apply(lambda bn: config.tags(Name=bn)),
                opts=child_opts,
            )
            self._buckets[suffix] = bucket

            aws.s3.BucketVersioning(
                f"{name}-{suffix}-versioning",
                bucket=bucket.id,
                versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
                    status="Enabled",
                ),
                opts=child_opts,
            )

            aws.s3.BucketPublicAccessBlock(
                f"{name}-{suffix}-public-access-block",
                bucket=bucket.id,
                block_public_acls=True,
                block_public_policy=True,
                ignore_public_acls=True,
                restrict_public_buckets=True,
                opts=child_opts,
            )

            if kms_key_arn:
                encryption_rules = [
                    aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                        apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                            sse_algorithm="aws:kms",
                            kms_master_key_id=kms_key_arn,
                        ),
                        bucket_key_enabled=True,
                    ),
                ]
            else:
                encryption_rules = [
                    aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                        apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                            sse_algorithm="AES256",
                        ),
                    ),
                ]
            aws.s3.BucketServerSideEncryptionConfiguration(
                f"{name}-{suffix}-encryption",
                bucket=bucket.id,
                rules=encryption_rules,
                opts=child_opts,
            )

            # Mirror the GCS lifecycle: abort day-old incomplete multiparts and
            # reap noncurrent versions after 7 days.
            aws.s3.BucketLifecycleConfiguration(
                f"{name}-{suffix}-lifecycle",
                bucket=bucket.id,
                rules=[
                    aws.s3.BucketLifecycleConfigurationRuleArgs(
                        id="abort-incomplete-multipart",
                        status="Enabled",
                        abort_incomplete_multipart_upload=aws.s3.BucketLifecycleConfigurationRuleAbortIncompleteMultipartUploadArgs(
                            days_after_initiation=1,
                        ),
                    ),
                    aws.s3.BucketLifecycleConfigurationRuleArgs(
                        id="expire-noncurrent-versions",
                        status="Enabled",
                        noncurrent_version_expiration=aws.s3.BucketLifecycleConfigurationRuleNoncurrentVersionExpirationArgs(
                            noncurrent_days=7,
                        ),
                    ),
                ],
                opts=child_opts,
            )

        # Dedicated IAM identity for the Nexus pods, scoped to just these buckets
        # rather than the node role's account-wide S3 access. Trusts exactly the
        # chart's KSAs (NEXUS_KSA_MEMBERS); the chart annotates those same KSAs
        # with eks.amazonaws.com/role-arn=<this role>.
        trust_policy = pulumi.Output.all(oidc_provider_arn, oidc_provider_url).apply(
            lambda args: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Federated": args[0]},
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Condition": {
                                "StringEquals": {
                                    f"{args[1].replace('https://', '')}:aud": "sts.amazonaws.com",
                                    f"{args[1].replace('https://', '')}:sub": [
                                        f"system:serviceaccount:{ns}:{ksa}"
                                        for ns, ksa in NEXUS_KSA_MEMBERS
                                    ],
                                }
                            },
                        }
                    ],
                }
            )
        )

        self._role = aws.iam.Role(
            f"{name}-role",
            name=resource_suffix.apply(lambda s: f"{config.resource_prefix}-nexus-s3-{s}"),
            assume_role_policy=trust_policy,
            tags=config.tags(Name=f"{config.resource_prefix}-nexus-s3"),
            opts=child_opts,
        )

        bucket_arns = [self._buckets[s].arn for s in _NEXUS_BUCKETS]
        aws.iam.RolePolicy(
            f"{name}-role-policy",
            role=self._role.id,
            policy=pulumi.Output.all(*bucket_arns).apply(
                lambda arns: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                                "Resource": list(arns),
                            },
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetObject",
                                    "s3:PutObject",
                                    "s3:DeleteObject",
                                    "s3:AbortMultipartUpload",
                                ],
                                "Resource": [f"{arn}/*" for arn in arns],
                            },
                        ],
                    }
                )
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self._role]),
        )

        self.register_outputs(
            {
                **{s: self._buckets[s].bucket for s in _NEXUS_BUCKETS},
                "role_arn": self._role.arn,
            }
        )

    @property
    def source(self) -> pulumi.Output[str]:
        return self._buckets["source"].bucket

    @property
    def knowledge(self) -> pulumi.Output[str]:
        return self._buckets["knowledge"].bucket

    @property
    def archive(self) -> pulumi.Output[str]:
        return self._buckets["archive"].bucket

    @property
    def role_arn(self) -> pulumi.Output[str]:
        """ARN of the IAM role the Nexus KSAs assume via IRSA."""
        return self._role.arn
