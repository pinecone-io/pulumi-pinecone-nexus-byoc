"""Bedrock via IRSA: only the inference proxy's KSA carries the Bedrock role."""

import json
import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.aws import AWSConfig  # noqa: E402
from pulumi_pinecone_byoc.aws.nexus_s3 import (  # noqa: E402
    irsa_trust_policy,
    nexus_bedrock_annotations,
)
from pulumi_pinecone_byoc.common.nexus import (  # noqa: E402
    NEXUS_INFERENCE_PROXY_KSA,
    Nexus,
    uses_aws_identity,
)

_ROLE = {"eks.amazonaws.com/role-arn": "arn:aws:iam::1:role/nexus-bedrock"}
_TOML = (
    '[llm_models.s]\napi_style = "litellm"\nmodel = "bedrock/x"\naws_region_name = "us-west-2"\n'
)


class _Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[str, dict] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.typ] = args.inputs
        return f"{args.name}-id", {**args.inputs, "arn": f"arn:aws:iam::123:role/{args.name}"}

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {"accountId": "123"}


@pulumi.runtime.test
def test_proxy_annotation_rides_beside_the_byoc_catalog():
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    provider = pulumi.ProviderResource("pulumi:providers:kubernetes", "k8s", {})
    nexus = Nexus(
        "t",
        k8s_provider=provider,
        image_registry="reg.example/nexus",
        nexus_version="1.2.3",
        byoc_env="e.byoc",
        cloud="aws",
        region="us-west-2",
        pinecone_prod=True,
        byoc_project_id="proj",
        inference_models_toml=_TOML,
        inference_proxy_service_account_annotations=_ROLE,
    )

    def check(data):
        proxy = json.loads(data["app-values.yaml"])["inference-proxy"]
        assert proxy["serviceAccountAnnotations"] == _ROLE
        assert proxy["byocConfigMap"]
        assert proxy["providerKeyRefs"] == []

    return nexus.deploy_values.data.apply(check)


@pulumi.runtime.test
def test_bedrock_trust_names_the_proxy_ksa_alone():
    def check(doc):
        subs = json.loads(doc)["Statement"][0]["Condition"]["StringEquals"]["oidc.example/x:sub"]
        assert subs == ["system:serviceaccount:nexus:nexus-inference-proxy"]

    return irsa_trust_policy(
        "arn:aws:iam::1:oidc-provider/x", "https://oidc.example/x", (NEXUS_INFERENCE_PROXY_KSA,)
    ).apply(check)


def test_aws_identity_follows_the_catalog():
    assert uses_aws_identity(_TOML)
    rerank_only = '[rerank_models.r]\napi_style = "litellm"\nmodel = "bedrock/r"\naws_region_name = "us-west-2"\n'
    assert uses_aws_identity(rerank_only)
    keyed = '[llm_models.s]\napi_style = "litellm"\nmodel = "anthropic/x"\napi_key_ref = "k"\n'
    assert not uses_aws_identity(keyed)


def _annotations(toml):
    return nexus_bedrock_annotations(
        "t",
        AWSConfig(region="us-west-2", availability_zones=["us-west-2a"]),
        toml,
        cell_name="cell-abcd",
        oidc_provider_arn="arn:aws:iam::123:oidc-provider/x",
        oidc_provider_url="https://oidc.example/x",
        opts=pulumi.ResourceOptions(),
    )


@pulumi.runtime.test
def test_keyed_catalog_gets_no_bedrock_role():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)
    keyed = '[llm_models.s]\napi_style = "litellm"\nmodel = "anthropic/x"\napi_key_ref = "k"\n'
    assert _annotations(keyed) is None
    assert _annotations(None) is None
    assert not mocks.resources


@pulumi.runtime.test
def test_bedrock_role_arn_lands_on_the_proxy_and_policy_is_scoped():
    mocks = _Mocks()
    pulumi.runtime.set_mocks(mocks, preview=False)

    def check(annotations):
        assert annotations == {"eks.amazonaws.com/role-arn": "arn:aws:iam::123:role/t-role"}
        invoke, rerank = json.loads(mocks.resources["aws:iam/rolePolicy:RolePolicy"]["policy"])[
            "Statement"
        ]
        assert invoke["Resource"] == [
            "arn:aws:bedrock:*::foundation-model/*",
            "arn:aws:bedrock:*:123:inference-profile/*",
        ]
        assert rerank == {"Effect": "Allow", "Action": "bedrock:Rerank", "Resource": "*"}

    return _annotations(_TOML).apply(check)


def test_aws_region_name_refused_off_aws():
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    try:
        Nexus(
            "t",
            k8s_provider=pulumi.ProviderResource("pulumi:providers:kubernetes", "k8s", {}),
            image_registry="reg.example/nexus",
            nexus_version="1.2.3",
            byoc_env="e.byoc",
            cloud="gcp",
            region="us-central1",
            pinecone_prod=True,
            byoc_project_id="proj",
            inference_models_toml=_TOML,
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for aws_region_name on gcp")
