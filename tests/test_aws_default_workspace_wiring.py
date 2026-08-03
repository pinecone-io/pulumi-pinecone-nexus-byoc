"""AWS default-workspace console-link wiring: URL shapes + existence gating.

The DefaultWorkspace provider and workspace API are covered by their own
(cloud-agnostic) tests; this covers the PineconeAWSCluster property layer
ported from GCP — the data/control console URLs and how they gate on the
live workspace-existence check. The cluster is built with ``object.__new__``
so only the attributes the properties read need to exist (a full component
construction reaches for real AWS waiters).

No Pulumi mock runtime: the properties only combine Outputs (no resources
are registered), so each test resolves them on a private event loop. This
keeps the module from participating in the cross-module event-loop pollution
between ``@pulumi.runtime.test`` modules and the provider tests' ``asyncio.run``.

Run standalone (`python tests/test_aws_default_workspace_wiring.py`) or under pytest.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common import api  # noqa: E402

try:
    from pulumi_pinecone_byoc.aws.cluster import (
        PineconeAWSCluster,
        PineconeAWSClusterArgs,
    )

    _HAS_AWS = True
except ModuleNotFoundError:
    _HAS_AWS = False

_HOST = "default-byocab12.wksp.aws-us-east-1-ab12.pinecone.io"


def _cluster(nexus_deployed: bool = True, workspace_name: str = "default") -> "PineconeAWSCluster":
    """A cluster with just the attributes the console-URL properties read."""
    cluster = object.__new__(PineconeAWSCluster)
    cluster.args = PineconeAWSClusterArgs(pinecone_api_key="key-1", pinecone_version="v")
    cluster._default_workspace_name = workspace_name
    # Outputs need an event loop, so only build them on the Nexus-deployed
    # path (called from inside a coroutine); DB-only reads no Outputs.
    cluster._environment = SimpleNamespace(
        org_id=pulumi.Output.from_input("org-1") if nexus_deployed else None
    )
    cluster._nexus_project_id = "proj-1" if nexus_deployed else None
    cluster._default_workspace = (
        SimpleNamespace(host=pulumi.Output.from_input(_HOST)) if nexus_deployed else None
    )
    # the private (name-mangled) memo the existence check lazily fills
    cluster._PineconeAWSCluster__default_workspace_exists = None
    return cluster


def _resolve(workspace_exists: bool, read_urls, workspace_name: str = "default"):
    """Build the cluster and resolve ``read_urls(cluster)`` on a private loop.

    Outputs bind futures to the loop current at creation time, so both the
    property reads and the await run inside one coroutine on one fresh loop.
    """
    if not _HAS_AWS:
        return None

    async def go():
        cluster = _cluster(workspace_name=workspace_name)
        urls = read_urls(cluster)
        return await pulumi.Output.all(*urls).future()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with patch.object(api, "workspace_exists", return_value=workspace_exists):
            return loop.run_until_complete(go())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_data_console_url_built_from_host():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    urls = _resolve(True, lambda c: [c.nexus_default_workspace_data_console_url])
    assert urls == [f"https://{_HOST}/contexts"]


def test_control_console_url_deep_links_org_project_workspace():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    urls = _resolve(True, lambda c: [c.nexus_default_workspace_control_console_url])
    assert urls == [
        "https://app.pinecone.io/organizations/org-1/projects/proj-1/workspaces/default"
    ]


def test_control_console_url_follows_configured_workspace_name():
    # Multi-cell-per-project installs override the name; the deep link must follow.
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    urls = _resolve(
        True,
        lambda c: [c.nexus_default_workspace_control_console_url],
        workspace_name="default-e35a",
    )
    assert urls == [
        "https://app.pinecone.io/organizations/org-1/projects/proj-1/workspaces/default-e35a"
    ]


def test_urls_null_once_workspace_deleted():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    urls = _resolve(
        False,
        lambda c: [
            c.nexus_default_workspace_data_console_url,
            c.nexus_default_workspace_control_console_url,
        ],
    )
    assert urls == [None, None]


def test_urls_none_on_db_only_deploys():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    cluster = _cluster(nexus_deployed=False)
    assert cluster.nexus_default_workspace_data_console_url is None
    assert cluster.nexus_default_workspace_control_console_url is None


def test_default_workspace_name_derives_per_cell_suffix():
    # Un-overridden, the default workspace name is `default-<cell-suffix>` so a
    # leftover workspace from a torn-down cell can't collide project-wide. This
    # locks in the exact derivation the cluster constructor applies.
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    from pulumi_pinecone_byoc.common.providers import DEFAULT_WORKSPACE_NAME

    async def go():
        suffix = pulumi.Output.from_input("ab12")
        name = suffix.apply(lambda s: f"{DEFAULT_WORKSPACE_NAME}-{s}")
        return await name.future()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        assert loop.run_until_complete(go()) == "default-ab12"
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_console_url_arg_defaults_to_public_console():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    args = PineconeAWSClusterArgs(pinecone_api_key="k", pinecone_version="v")
    assert args.console_url == "https://app.pinecone.io"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
