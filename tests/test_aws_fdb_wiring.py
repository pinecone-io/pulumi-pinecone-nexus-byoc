"""AWS shared-FDB wiring: the fdb data-plane backend on EKS (nexus#1378).

Covers the PineconeAWSCluster layer added when the #32 external-FDB guard was
replaced with the real wiring:

- the AZ-count guard: with <3 AZs the FoundationDB CR silently degrades from
  zone to hostname fault domains, so the fdb backend rejects short AZ lists;
- the RDS-less property layer: fdb cells provision no RDS, so every
  RDS-backed property must read as None instead of raising.

The Nexus deploy-values contract for ``fdb_mode`` and the cross-cloud
``external``-requires-``fdb`` arg guards live in ``test_nexus_fdb_external.py``.
The cluster here is built with ``object.__new__`` so only the attributes the
properties read need to exist (a full component construction reaches for real
AWS waiters) — no Pulumi mock runtime, keeping this module out of the
cross-module event-loop pollution between ``@pulumi.runtime.test`` modules.

Run standalone (`python tests/test_aws_fdb_wiring.py`) or under pytest.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pulumi_pinecone_byoc.aws.cluster import (
        PineconeAWSCluster,
        PineconeAWSClusterArgs,
    )

    _HAS_AWS = True
except ModuleNotFoundError:
    _HAS_AWS = False


def test_default_availability_zones_are_three():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    args = PineconeAWSClusterArgs(pinecone_api_key="k", pinecone_version="v")
    assert len(args.availability_zones) == 3


def test_fdb_backend_rejects_fewer_than_three_azs():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    try:
        PineconeAWSClusterArgs(
            pinecone_api_key="k",
            pinecone_version="v",
            data_plane_backend="fdb",
            availability_zones=["us-east-1a", "us-east-1b"],
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: fdb backend needs >= 3 AZs")


def test_fdb_backend_accepts_three_azs():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    args = PineconeAWSClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        data_plane_backend="fdb",
    )
    assert args.data_plane_backend == "fdb"


def test_postgres_backend_accepts_two_azs():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    args = PineconeAWSClusterArgs(
        pinecone_api_key="k",
        pinecone_version="v",
        availability_zones=["us-east-1a", "us-east-1b"],
    )
    assert args.data_plane_backend == "postgres"


def _rdsless_cluster() -> "PineconeAWSCluster":
    """A cluster with just the attribute the RDS-backed properties read."""
    cluster = object.__new__(PineconeAWSCluster)
    cluster._rds = None
    return cluster


def test_rds_properties_none_on_fdb_cells():
    if not _HAS_AWS:
        print("  (skipped: pulumi_aws not installed)")
        return
    cluster = _rdsless_cluster()
    assert cluster.rds is None
    assert cluster.control_db is None
    assert cluster.system_db is None
    assert cluster.control_db_endpoint is None
    assert cluster.system_db_endpoint is None
    assert cluster.control_db_connection_secret_arn is None
    assert cluster.system_db_connection_secret_arn is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
