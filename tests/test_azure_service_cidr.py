"""Azure AKS service CIDR must be private and clear of the VNet.

The cluster ClusterIP range used to be 112.0.0.0/16 -- public APNIC space, so
pods black-holed any real host in that range (nexus#1404). It must be RFC1918
(like the GCP/AWS cells) and not overlap the default 10.0.0.0/16 VNet, and the
kube-dns service IP must sit inside the service range.

Run standalone (`python tests/test_azure_service_cidr.py`) or under pytest.
"""

import ipaddress
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pulumi_pinecone_byoc.azure.aks import _DNS_SERVICE_IP, _SERVICE_CIDR

    _HAS_AZURE = True
except ModuleNotFoundError:
    _HAS_AZURE = False

_VNET_CIDR = ipaddress.ip_network("10.0.0.0/16")


def test_service_cidr_is_private():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    net = ipaddress.ip_network(_SERVICE_CIDR)
    assert net.is_private, f"{_SERVICE_CIDR} is not RFC1918/private"


def test_service_cidr_does_not_overlap_vnet():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    assert not ipaddress.ip_network(_SERVICE_CIDR).overlaps(_VNET_CIDR)


def test_dns_service_ip_is_inside_the_service_cidr():
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
        return
    assert ipaddress.ip_address(_DNS_SERVICE_IP) in ipaddress.ip_network(_SERVICE_CIDR)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
