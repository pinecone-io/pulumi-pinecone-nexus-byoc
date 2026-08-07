"""Nexus pods must carry the pool nodeSelectors in the deploy values.

The nexus Helm chart leaves ``scheduling.*.nodeSelector`` empty by default, so
the computed BYOC values must set them to match the labels on the dedicated
``nexus-services`` / ``nexus-jobs`` pools (see ``nexus_node_pools`` in the
per-cloud cluster modules). Without them a Nexus pod has no pool affinity and
can schedule onto the untainted default/DB pool, breaking pool isolation.
Tolerations are supplied by the chart base and are not asserted here.

Run standalone (`python tests/test_nexus_scheduling.py`) or under pytest.
"""

import json
import os
import sys
from typing import Literal

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _deploy_values(fdb_mode: Literal["single", "external"]):
    """Instantiate Nexus in the given mode; return the deploy-values ``data`` Output."""
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
        fdb_mode=fdb_mode,
    )
    return nexus.deploy_values.data


@pulumi.runtime.test
def test_app_values_pin_services_and_jobs_pools():
    def check(data):
        app_values = json.loads(data["app-values.yaml"])
        assert app_values["scheduling"]["services"]["nodeSelector"] == {"nexus-role": "services"}
        assert app_values["scheduling"]["jobs"]["nodeSelector"] == {"nexus-role": "jobs"}

    return _deploy_values("single").apply(check)


@pulumi.runtime.test
def test_fdb_values_pin_services_pool():
    def check(data):
        fdb_values = json.loads(data["fdb-values.yaml"])
        assert fdb_values["scheduling"]["services"]["nodeSelector"] == {"nexus-role": "services"}

    return _deploy_values("single").apply(check)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
