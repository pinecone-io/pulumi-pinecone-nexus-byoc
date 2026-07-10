"""Pure selection logic for the internal LB's forwarding rule.

Kept free of pulumi/provider imports so it is unit-testable without the GCP
SDK stack (see tests/test_internal_lb_selection.py).
"""


def ingress_ip_from_status(status) -> str | None:
    """Return the LB IP from a k8s Ingress status dict, or None if absent.

    The Pulumi k8s provider surfaces status with snake_case keys:
    {"load_balancer": {"ingress": [{"ip": ...}]}}.
    """
    try:
        ip = status["load_balancer"]["ingress"][0]["ip"]
    except (KeyError, IndexError, TypeError):
        return None
    return ip or None


def select_forwarding_rule(rules, cell_name: str, ingress_ip: str | None):
    """Pick the cell's private-ingress forwarding rule from a regional listing.

    Two internal LBs share the cell subnet (the private Gloo LB and the Nexus
    gateway ingress), so the ingress IP is the only deterministic key. When
    the IP is known, returns the in-subnet rule carrying it, or None if that
    rule isn't listed yet (caller retries). Without an IP, falls back to the
    first in-subnet rule — ambiguous, so the caller should warn.
    """
    candidates = [r for r in rules if r.subnetwork and r.subnetwork.endswith(cell_name)]
    if ingress_ip is not None:
        return next((r for r in candidates if r.ip_address == ingress_ip), None)
    return candidates[0] if candidates else None
