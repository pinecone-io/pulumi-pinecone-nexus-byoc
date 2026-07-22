"""Azure AKS service CIDR must be private and clear of the VNet.

The ClusterIP range must be RFC1918 (an address inside the range is routed as a
ClusterIP, so the real host at that address is never reached) and must not
overlap the default 10.0.0.0/16 VNet, and the kube-dns service IP must sit
inside the service range. The cluster args reject a `vpc_cidr` that overlaps the
service range, so a custom VNet collision fails at construction instead of
silently breaking connectivity.

Run standalone (`python tests/test_azure_service_cidr.py`) or under pytest.
"""

import ipaddress
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pulumi_pinecone_byoc.azure.aks import DNS_SERVICE_IP, SERVICE_CIDR
    from pulumi_pinecone_byoc.azure.cluster import PineconeAzureClusterArgs

    _HAS_AZURE = True
except ModuleNotFoundError:
    _HAS_AZURE = False

_VNET_CIDR = ipaddress.ip_network("10.0.0.0/16")


def test_service_cidr_is_private():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    net = ipaddress.ip_network(SERVICE_CIDR)
    assert net.is_private, f"{SERVICE_CIDR} is not RFC1918/private"


def test_service_cidr_does_not_overlap_vnet():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    assert not ipaddress.ip_network(SERVICE_CIDR).overlaps(_VNET_CIDR)


def test_dns_service_ip_is_inside_the_service_cidr():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    assert ipaddress.ip_address(DNS_SERVICE_IP) in ipaddress.ip_network(SERVICE_CIDR)


def test_default_vpc_cidr_is_accepted():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    # the default 10.0.0.0/16 VNet must be clear of the service range
    args = PineconeAzureClusterArgs(pinecone_api_key="k", pinecone_version="v")
    assert not ipaddress.ip_network(args.vpc_cidr).overlaps(ipaddress.ip_network(SERVICE_CIDR))


def test_vpc_cidr_overlapping_service_cidr_is_rejected():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    try:
        PineconeAzureClusterArgs(
            pinecone_api_key="k",
            pinecone_version="v",
            vpc_cidr=SERVICE_CIDR,
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: vpc_cidr overlaps the service CIDR")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
