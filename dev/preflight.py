#!/usr/bin/env python3
"""Pinecone BYOC preflight — run before an install iteration.

A thin wrapper over the shared preflight checks (`preflight_checks.py`) plus the
setup wizard's own cloud-side checker, so cloud-side verification never drifts
from the real install path. The same UNIQUE checks now also run inside the
wizard (fail-fast); this standalone tool stays useful for re-running them on
demand against an already-generated project.

It adds, on top of the wizard's cloud-side `GCPPreflightChecker`:

  - host tools + live auth, the Pulumi backend/SSO session, IAM roles/owner,
    Pinecone key liveness (all in `preflight_checks.py`), and
  - the deploy-time reserved-CIDR guard + free-/12 suggestion (here), since the
    wizard's checker omits them.

Usage (--no-project avoids building the repo's own pulumi package):
    uv run --no-project --with rich --with pyyaml python dev/preflight.py
    uv run --no-project --with rich --with pyyaml python dev/preflight.py --nexus --stack-dir ../pinecone-byoc

Project/region/zones/CIDR default to the active gcloud config + the GCP wizard
defaults; override any of them with the matching flag. The impersonation/RAPT
check is OFF unless ADC is impersonation-based or --impersonate-service-account
is passed.
"""

from __future__ import annotations

import argparse
import ipaddress
import subprocess
import sys
from pathlib import Path

# This internal dev tool lives in dev/, but imports the shared checks + the
# wizard from setup/. preflight_checks holds the UNIQUE checks both this tool
# and the wizard call; the wizard supplies GCPPreflightChecker for the
# cloud-side run. Put setup/ on sys.path so the flat imports below resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))
from preflight_checks import (  # noqa: E402
    adc_is_impersonated,
    check_auth,
    check_gcp_rapt,
    check_host_tools,
    check_iam_owner,
    check_nexus_secrets,
    check_pinecone_api_key,
    check_pulumi_session,
    console,
    fail,
    gcloud_active_project,
    ok,
    section,
)
from wizard import GCPPreflightChecker  # noqa: E402

DEFAULT_REGION = "us-central1"
DEFAULT_CIDR = "10.112.0.0/12"

# Reserved ranges the deploy hard-rejects (pulumi_pinecone_byoc/gcp/vpc.py:25-35)
# but the wizard's preflight does NOT check -- mirror them here so preflight
# stays predictive of `pulumi up`.
RESERVED_SUBNETS = ["10.100.1.0/24", "10.100.2.0/24"]  # PSC, regional managed proxy
RESERVED_BLOCK = "10.100.0.0/16"  # broader guard used when suggesting a free range


def _in_region_subnet_ranges(
    project: str, region: str
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Primary + secondary subnet ranges in-region, across all networks.

    Used only to compute a suggestion -- the conflict *decision* stays with the
    wizard's checker. Including secondary ranges (GKE pods/services) keeps the
    suggestion clear of ranges a future cluster might auto-allocate.
    """
    ranges: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        r = subprocess.run(
            [
                "gcloud",
                "compute",
                "networks",
                "subnets",
                "list",
                f"--project={project}",
                f"--regions={region}",
                "--format=value(ipCidrRange,secondaryIpRanges[].ipCidrRange)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            for line in r.stdout.split("\n"):
                # list fields come back ';'-joined, columns tab-joined
                for tok in line.replace(";", " ").replace(",", " ").split():
                    try:
                        ranges.append(ipaddress.ip_network(tok.strip()))
                    except ValueError:
                        continue
    except Exception:
        pass
    return ranges


def _suggest_free_cidr(project: str, region: str) -> str | None:
    """Lowest /12 in 10.0.0.0/8 that overlaps no in-region subnet or reserved range."""
    blocked = _in_region_subnet_ranges(project, region)
    blocked.append(ipaddress.ip_network(RESERVED_BLOCK))
    for octet in range(0, 256, 16):  # /12s align on multiples of 16 in octet 2
        cand = ipaddress.ip_network(f"10.{octet}.0.0/12")
        if not any(cand.overlaps(b) for b in blocked):
            return str(cand)
    return None


def _check_reserved_overlap(cidr: str) -> bool:
    """Mirror vpc.py's deploy-time guard: vpc_cidr must not hit PSC/proxy ranges."""
    try:
        net = ipaddress.ip_network(cidr)
    except ValueError:
        return True  # invalid CIDR is already reported by the wizard's checker
    hits = [r for r in RESERVED_SUBNETS if net.overlaps(ipaddress.ip_network(r))]
    if hits:
        fail(
            f"VPC CIDR Reserved: {cidr} overlaps reserved {', '.join(hits)} "
            "(PSC / proxy) — the deploy rejects this",
            "Choose a CIDR outside 10.100.0.0/16",
        )
        return False
    ok(f"VPC CIDR Reserved: {cidr} clear of 10.100.0.0/16")
    return True


def check_cloud_side(project: str, region: str, zones: list[str], cidr: str) -> bool:
    section(f"GCP cloud-side (project {project}, region {region})")
    console.print(f"  [dim]zones: {', '.join(zones)} | cidr: {cidr}[/]")
    console.print()
    # delegate the conflict decision to the wizard's checker -- prints its own
    # per-check lines and returns True only if every check passed (no drift).
    checker = GCPPreflightChecker(project, region, zones, cidr)
    passed = checker.run_checks()

    # the deploy-time reserved-range guard the wizard's preflight omits.
    reserved_ok = _check_reserved_overlap(cidr)

    # if the CIDR is unusable for either reason, suggest a free /12.
    cidr_result = next((r for r in checker.results if r.name == "VPC CIDR"), None)
    cidr_blocked = (cidr_result is not None and not cidr_result.passed) or not reserved_ok
    if cidr_blocked:
        suggestion = _suggest_free_cidr(project, region)
        if suggestion:
            console.print(f"    [dim]→ free range available: --cidr {suggestion}[/]")
        else:
            console.print("    [dim]→ no free /12 in 10.0.0.0/8; pick a smaller block manually[/]")

    return passed and reserved_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Pinecone BYOC preflight checks (GCP).")
    parser.add_argument("--project", help="GCP project id (default: active gcloud config)")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"default: {DEFAULT_REGION}")
    parser.add_argument(
        "--zones",
        help="comma-separated zones (default: <region>-a,<region>-b)",
    )
    parser.add_argument("--cidr", default=DEFAULT_CIDR, help=f"default: {DEFAULT_CIDR}")
    parser.add_argument("--nexus", action="store_true", help="also check Nexus provider secrets")
    parser.add_argument(
        "--stack-dir",
        help="generated project dir to verify Nexus secrets against (implies --nexus)",
    )
    parser.add_argument(
        "--impersonate-service-account",
        dest="impersonate_sa",
        help=(
            "opt in to the GCP RAPT/impersonation check against this SA. Off by "
            "default; auto-enabled when ADC is already impersonation-based."
        ),
    )
    args = parser.parse_args()

    project = args.project or gcloud_active_project()
    region = args.region
    zones = (
        [z.strip() for z in args.zones.split(",") if z.strip()]
        if args.zones
        else [f"{region}-a", f"{region}-b"]
    )
    cidr = args.cidr
    nexus = args.nexus or bool(args.stack_dir)

    console.print()
    console.print("  [bold blue]Pinecone BYOC Preflight[/]")

    results: list[bool] = []
    results.append(check_host_tools())
    results.append(check_auth())
    # RAPT is gated: run only with an explicit SA or impersonation-based ADC.
    if args.impersonate_sa or adc_is_impersonated():
        results.append(check_gcp_rapt(args.impersonate_sa))
    results.append(check_pulumi_session(args.stack_dir))
    results.append(check_pinecone_api_key())

    if not project:
        section("GCP cloud-side")
        fail("No project id (pass --project or set one: gcloud config set project <id>)")
        results.append(False)
    else:
        results.append(check_iam_owner(project))
        results.append(check_cloud_side(project, region, zones, cidr))

    if nexus:
        results.append(check_nexus_secrets(args.stack_dir))

    section("Summary")
    # prerequisites from the official BYOC docs that this script cannot verify:
    console.print("  [dim]Not auto-checked (verify manually):[/]")
    console.print("    [dim]· Pinecone Enterprise plan (required for BYOC access)[/]")
    console.print(
        "    [dim]· After installing any new tool, open a new terminal so PATH picks it up[/]"
    )
    console.print()
    if all(results):
        ok("All preflight checks passed — ready to run the wizard / pulumi up.")
        return 0
    fail("One or more preflight checks failed. Fix the items above before iterating.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
