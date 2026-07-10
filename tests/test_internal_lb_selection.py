"""Forwarding-rule selection for the internal LB.

The cell subnet hosts two internal LBs (the private Gloo LB and the Nexus
gateway ingress), so selection must key on the private ingress's IP, not on
forwarding-rule listing order.

Run standalone (`python tests/test_internal_lb_selection.py`) or under pytest.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pulumi_pinecone_byoc", "gcp"))

# imported via the sys.path insert above so this file runs without the gcp
# extras (pulumi_pinecone_byoc.gcp's __init__ pulls in pulumi_gcp)
from lb_selection import (  # noqa: E402  # ty: ignore[unresolved-import]
    ingress_ip_from_status,
    select_forwarding_rule,
)

CELL = "cell-7650"
CELL_SUBNET = (
    f"https://www.googleapis.com/compute/v1/projects/p/regions/us-central1/subnetworks/{CELL}"
)
OTHER_SUBNET = (
    "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1/subnetworks/other"
)

GLOO_IP = "10.128.0.5"
NEXUS_IP = "10.128.0.7"


def _rule(name: str, ip: str, subnetwork: str | None = CELL_SUBNET) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, ip_address=ip, subnetwork=subnetwork, self_link=f"link/{name}"
    )


def _nexus_rule() -> SimpleNamespace:
    return _rule("k8s2-fr-abcd1234-nexus-nexus-gateway-wxyz", NEXUS_IP)


def _gloo_rule() -> SimpleNamespace:
    return _rule("k8s2-fs-abcd1234-gloo-system-private-gloo-lb-wxyz", GLOO_IP)


def test_picks_gloo_rule_by_ip_even_when_nexus_lists_first():
    rules = [_nexus_rule(), _gloo_rule()]
    selected = select_forwarding_rule(rules, CELL, GLOO_IP)
    assert selected is rules[1]


def test_returns_none_when_ip_known_but_rule_not_listed_yet():
    assert select_forwarding_rule([_nexus_rule()], CELL, GLOO_IP) is None


def test_falls_back_to_first_in_subnet_when_ip_unavailable():
    rules = [_nexus_rule(), _gloo_rule()]
    assert select_forwarding_rule(rules, CELL, None) is rules[0]


def test_ignores_rules_outside_the_cell_subnet():
    outsider = _rule("k8s2-fs-other-private-gloo-lb", GLOO_IP, subnetwork=OTHER_SUBNET)
    subnetless = _rule("global-rule", GLOO_IP, subnetwork=None)
    assert select_forwarding_rule([outsider, subnetless], CELL, GLOO_IP) is None
    assert select_forwarding_rule([outsider, subnetless], CELL, None) is None


def test_ingress_ip_from_status_reads_snake_case_keys():
    status = {"load_balancer": {"ingress": [{"ip": GLOO_IP}]}}
    assert ingress_ip_from_status(status) == GLOO_IP


def test_ingress_ip_from_status_is_none_on_missing_or_empty_shapes():
    assert ingress_ip_from_status(None) is None
    assert ingress_ip_from_status({}) is None
    assert ingress_ip_from_status({"load_balancer": None}) is None
    assert ingress_ip_from_status({"load_balancer": {"ingress": []}}) is None
    assert ingress_ip_from_status({"load_balancer": {"ingress": [{}]}}) is None
    assert ingress_ip_from_status({"load_balancer": {"ingress": [{"ip": ""}]}}) is None


if __name__ == "__main__":
    for fn_name, fn in sorted(globals().items()):
        if fn_name.startswith("test_"):
            fn()
    print("ok")
