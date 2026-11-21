"""A cell that must not report billable Nexus answers can switch counting off.

Nexus counts answers unless ``config.countAnswers`` is false. Run standalone
(`python tests/test_nexus_count_answers.py`) or under pytest.
"""

import json
import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _app_config(**kwargs):
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    provider = pulumi.ProviderResource("pulumi:providers:kubernetes", "k8s", {})
    nexus = Nexus(
        "t",
        k8s_provider=provider,
        image_registry="reg.example/nexus",
        nexus_version="1.2.3",
        byoc_env="e.byoc",
        cloud="gcp",
        region="us-central1",
        pinecone_prod=True,
        byoc_project_id="proj",
        **kwargs,
    )
    return nexus.deploy_values.data.apply(lambda d: json.loads(d["app-values.yaml"])["config"])


@pulumi.runtime.test
def test_counting_is_left_to_nexus_by_default():
    def check(config):
        assert "countAnswers" not in config

    return _app_config().apply(check)


@pulumi.runtime.test
def test_counting_can_be_switched_off():
    def check(config):
        assert config["countAnswers"] is False

    return _app_config(count_answers=False).apply(check)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
