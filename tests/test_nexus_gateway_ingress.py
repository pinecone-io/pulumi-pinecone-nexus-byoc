"""Gateway Ingress creation is gated on ingress_class.

GKE passes ``gce-internal`` and keeps its Ingress (an in-VPC ops door onto the
gateway). EKS/AKS pass ``ingress_class=None`` and get NO Ingress: no controller
serves a class-less Ingress on those clusters, so it would sit inert — the
gateway is reached in-cluster via the netstack *.wksp route (nexus#1362).

Run standalone (`python tests/test_nexus_gateway_ingress.py`) or under pytest.
"""

import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _nexus(ingress_class: str | None) -> Nexus:
    # Set within the runtime test (event loop active) rather than at import time.
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    provider = pulumi.ProviderResource("pulumi:providers:kubernetes", "k8s", {})
    return Nexus(
        "t",
        k8s_provider=provider,
        image_registry="reg.example/nexus",
        nexus_version="1.2.3",
        byoc_env="e.byoc",
        cloud="aws",
        region="us-east-1",
        pinecone_prod=True,
        byoc_project_id="proj",
        ingress_class=ingress_class,
    )


@pulumi.runtime.test
def test_no_ingress_class_creates_no_ingress():
    nexus = _nexus(None)
    assert nexus.gateway_ingress is None


@pulumi.runtime.test
def test_ingress_class_creates_classed_ingress():
    nexus = _nexus("gce-internal")
    assert nexus.gateway_ingress is not None

    def check(annotations):
        assert annotations["kubernetes.io/ingress.class"] == "gce-internal"
        assert annotations["kubernetes.io/ingress.allow-http"] == "true"
        # skipAwait was the workaround for the never-ready class-less Ingress;
        # a classed Ingress must keep Pulumi's readiness await.
        assert "pulumi.com/skipAwait" not in annotations

    return nexus.gateway_ingress.metadata.apply(lambda m: check(m["annotations"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
