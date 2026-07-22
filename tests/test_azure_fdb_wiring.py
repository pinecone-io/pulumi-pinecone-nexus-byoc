"""Azure shared-FDB wiring: the fdb data-plane backend on AKS (nexus#1404).

Azure port of the AWS wiring (nexus#1378). Covers the PineconeAzureCluster
layer:

- the zone-count guard: with <3 zones the FoundationDB CR silently degrades
  from zone to hostname fault domains, so the fdb backend rejects short zone
  lists;
- the Database-less property layer: fdb cells provision no Flexible Server, so
  the ``database`` property must read as None instead of raising.

The Nexus deploy-values contract for ``fdb_mode`` and the cross-cloud
``external``-requires-``fdb`` arg guards live in ``test_nexus_fdb_external.py``.
The cluster here is built with ``object.__new__`` so only the attributes the
properties read need to exist (a full component construction reaches for real
Azure waiters).

Run standalone (`python tests/test_azure_fdb_wiring.py`) or under pytest.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pulumi_pinecone_byoc.azure.cluster import (
        PineconeAzureCluster,
        PineconeAzureClusterArgs,
    )

    _HAS_AZURE = True
except ModuleNotFoundError:
    _HAS_AZURE = False


def test_default_availability_zones_are_three():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    args = PineconeAzureClusterArgs(pinecone_api_key="k", pinecone_version="v")
    assert len(args.availability_zones) == 3


def test_fdb_backend_rejects_fewer_than_three_zones():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    try:
        PineconeAzureClusterArgs(
            pinecone_api_key="k",
            pinecone_version="v",
            data_plane_backend="fdb",
            availability_zones=["1", "2"],
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: fdb backend needs >= 3 zones")


def test_fdb_backend_accepts_three_zones():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    args = PineconeAzureClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        data_plane_backend="fdb",
    )
    assert args.data_plane_backend == "fdb"


def test_postgres_backend_accepts_two_zones():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    args = PineconeAzureClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        availability_zones=["1", "2"],
    )
    assert args.data_plane_backend == "postgres"


def _databaseless_cluster() -> "PineconeAzureCluster":
    """A cluster with just the attribute the Database-backed property reads."""
    cluster = object.__new__(PineconeAzureCluster)
    cluster._database = None
    return cluster


def test_database_property_none_on_fdb_cells():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    cluster = _databaseless_cluster()
    assert cluster.database is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
