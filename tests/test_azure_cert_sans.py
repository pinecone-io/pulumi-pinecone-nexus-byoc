"""Azure netstack TLS SANs: the cert Gloo serves must cover the wksp host.

Gloo terminates workspace TLS with this Let's Encrypt cert, so the ``*.wksp``
host needs its own SAN or it fails TLS even on the public path. The
private-endpoint ``*.wksp.private`` SAN is a separate gap and intentionally
absent.

The Ingress is built inside the InternalLoadBalancer component, so this
constructs it under the Pulumi mock runtime and reads back the TLS host set.

Run standalone (`python tests/test_azure_cert_sans.py`) or under pytest.
"""

import os
import sys

import pulumi
import pulumi_kubernetes as k8s

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from config.azure import AzureConfig
    from pulumi_pinecone_byoc.azure.nlb import InternalLoadBalancer

    _HAS_AZURE = True
except ModuleNotFoundError:
    _HAS_AZURE = False

_SUBDOMAIN = "azure-us-east-1-ab12.pinecone.io"


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _nlb() -> InternalLoadBalancer:
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    config = AzureConfig(
        subscription_id="sub-1", region="eastus", availability_zones=["1", "2", "3"]
    )
    return InternalLoadBalancer(
        "t",
        config=config,
        k8s_provider=k8s.Provider("k8s"),
        resource_group_name="rg",
        pls_subnet_name=pulumi.Output.from_input("pls-subnet"),
        dns_zone_name=pulumi.Output.from_input(_SUBDOMAIN),
        subdomain=pulumi.Output.from_input(_SUBDOMAIN),
        external_ip_address=pulumi.Output.from_input("203.0.113.1"),
        cell_name="azure-us-east-1-ab12",
    )


@pulumi.runtime.test
def test_wksp_host_is_in_the_cert_sans():
    nlb = _nlb()

    def check(spec):
        hosts = set(spec["tls"][0]["hosts"])
        assert f"*.wksp.{_SUBDOMAIN}" in hosts
        # the four pre-existing SANs stay, and the private wksp gap stays out
        assert f"*.{_SUBDOMAIN}" in hosts
        assert f"*.svc.{_SUBDOMAIN}" in hosts
        assert f"*.private.{_SUBDOMAIN}" in hosts
        assert f"*.svc.private.{_SUBDOMAIN}" in hosts
        assert f"*.wksp.private.{_SUBDOMAIN}" not in hosts

    return nlb._ingress.spec.apply(check)


if __name__ == "__main__":
    if not _HAS_AZURE:
        print("  (skipped: pulumi_azure_native not installed)")
    else:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"ok  {name}")
        print("all passed")
