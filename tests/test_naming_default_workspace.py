"""Unit tests for the cloud-agnostic ``default_workspace_name`` naming helper.

The three cluster constructors (GCP/AWS/EKS) all resolve the first-run workspace
name through this one helper, so its two branches are pinned here directly rather
than through each cloud's component. No Pulumi mock runtime: the helper only
builds Outputs, so each case is resolved on a private event loop.

Run standalone (`python tests/test_naming_default_workspace.py`) or under pytest.
"""

import asyncio
import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.naming import default_workspace_name  # noqa: E402


def _resolve(explicit, suffix):
    async def go():
        return await default_workspace_name(explicit, pulumi.Output.from_input(suffix)).future()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(go())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_explicit_name_passes_through():
    # An operator-supplied name wins verbatim -- no suffix is appended.
    assert _resolve("default-e35a", "ab12") == "default-e35a"


def test_unset_name_derives_per_cell_suffix():
    # Un-overridden, the name is `default-<cell-suffix>` so a leftover workspace
    # from a torn-down cell can't collide project-wide.
    assert _resolve(None, "ab12") == "default-ab12"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
