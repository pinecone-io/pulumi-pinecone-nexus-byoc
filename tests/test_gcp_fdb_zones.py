"""GCP fdb data-plane zone handling: the ≥3-zone guard and 3-zone defaults.

GCP port of the AWS shared-FDB wiring guard: with fewer than 3 zones
the FoundationDB CR silently degrades from zone to hostname fault domains, and
nothing downstream validates it. The shared-FDB wiring itself (backend arg,
AlloyDB skip, fdb_mode passthrough) landed in #10 and is covered by
``test_nexus_fdb_external.py``; this covers only the new zone-count guard.

Run standalone (`python tests/test_gcp_fdb_zones.py`) or under pytest.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pulumi_pinecone_byoc.gcp.cluster import PineconeGCPClusterArgs

    _HAS_GCP = True
except ModuleNotFoundError:
    _HAS_GCP = False


def test_default_availability_zones_are_three():
    if not _HAS_GCP:
        print("  (skipped: pulumi_gcp not installed)")
        return
    args = PineconeGCPClusterArgs(pinecone_api_key="k", pinecone_version="v", project="p")
    assert len(args.availability_zones) == 3


def test_fdb_backend_rejects_fewer_than_three_zones():
    if not _HAS_GCP:
        print("  (skipped: pulumi_gcp not installed)")
        return
    try:
        PineconeGCPClusterArgs(
            pinecone_api_key="k",
            pinecone_version="v",
            project="p",
            data_plane_backend="fdb",
            availability_zones=["us-central1-a", "us-central1-b"],
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: fdb backend needs >= 3 zones")


def test_fdb_backend_accepts_three_zones():
    if not _HAS_GCP:
        print("  (skipped: pulumi_gcp not installed)")
        return
    args = PineconeGCPClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        project="p",
        data_plane_backend="fdb",
    )
    assert args.data_plane_backend == "fdb"


def test_postgres_backend_accepts_two_zones():
    if not _HAS_GCP:
        print("  (skipped: pulumi_gcp not installed)")
        return
    args = PineconeGCPClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        project="p",
        availability_zones=["us-central1-a", "us-central1-b"],
    )
    assert args.data_plane_backend == "postgres"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
