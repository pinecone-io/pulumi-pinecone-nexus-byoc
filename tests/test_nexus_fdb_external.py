"""External FDB mode: Nexus consumes the shared FDB data-plane cluster.

Covers the PR-2 contract for `fdb_mode="external"`:
- the deploy-values ConfigMap signals the installer to skip the FDB charts
  (fdb-values `.foundationdb.mode = external`) and points the nexus chart at the
  shared cluster file (app-values `.foundationdb.source = external`);
- validation rejects `external` unless the data plane is `fdb`.

Run standalone (`python tests/test_nexus_fdb_external.py`) or under pytest.
"""

import json
import os
import sys
from typing import Literal

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus, NexusConfig  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _deploy_values(fdb_mode: Literal["single", "external"]):
    """Instantiate Nexus in the given mode; return the deploy-values ``data`` Output."""
    # Set within the runtime test (event loop active) rather than at import time.
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
def test_external_skips_fdb_chart_and_points_at_shared_cluster():
    def check(data):
        fdb_values = json.loads(data["fdb-values.yaml"])
        app_values = json.loads(data["app-values.yaml"])
        assert fdb_values["foundationdb"]["mode"] == "external"
        assert app_values["foundationdb"] == {"source": "external"}

    return _deploy_values("external").apply(check)


@pulumi.runtime.test
def test_single_mode_emits_no_foundationdb_mode_or_source():
    def check(data):
        fdb_values = json.loads(data["fdb-values.yaml"])
        app_values = json.loads(data["app-values.yaml"])
        assert "mode" not in fdb_values["foundationdb"]
        assert "foundationdb" not in app_values

    return _deploy_values("single").apply(check)


def test_config_accepts_external():
    assert NexusConfig(fdb_mode="external").fdb_mode == "external"


def test_config_rejects_unknown_mode():
    try:
        NexusConfig(fdb_mode="bogus")  # type: ignore[arg-type]
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown fdb_mode")


def test_cluster_args_reject_external_on_postgres():
    try:
        from pulumi_pinecone_byoc.gcp import PineconeGCPClusterArgs
    except ModuleNotFoundError:
        print("  (skipped: pulumi_gcp not installed)")
        return

    try:
        PineconeGCPClusterArgs(
            pinecone_api_key="k",
            pinecone_version="v",
            project="p",
            data_plane_backend="postgres",
            nexus=NexusConfig(fdb_mode="external"),
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: external requires data_plane_backend=fdb")


def test_cluster_args_allow_external_on_fdb():
    try:
        from pulumi_pinecone_byoc.gcp import PineconeGCPClusterArgs
    except ModuleNotFoundError:
        print("  (skipped: pulumi_gcp not installed)")
        return

    args = PineconeGCPClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        project="p",
        data_plane_backend="fdb",
        nexus=NexusConfig(fdb_mode="external"),
    )
    assert args.nexus is not None and args.nexus.fdb_mode == "external"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
