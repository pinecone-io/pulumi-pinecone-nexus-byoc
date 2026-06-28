#!/usr/bin/env python3
"""Pinecone BYOC preflight — run before an install iteration.

A thin wrapper over the setup wizard's own checks so cloud-side verification
never drifts from the real install path. It runs on demand (the wizard's
preflight is only reachable partway through the interactive flow) and adds the
two things the wizard does NOT verify:

  - host tools + live auth (otherwise only checked inside bootstrap.sh /
    _validate_gcp_creds), and
  - Nexus provider secrets (the wizard only PRINTS reminders for these, so a
    missing key surfaces at `pulumi up` instead of here).

Usage (--no-project avoids building the repo's own pulumi package):
    uv run --no-project --with rich --with pyyaml python setup/preflight.py
    uv run --no-project --with rich --with pyyaml python setup/preflight.py --nexus --stack-dir ../pinecone-byoc

Project/region/zones/CIDR default to the active gcloud config + the GCP wizard
defaults; override any of them with the matching flag.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# import the wizard from this directory (same dir as this script) to reuse its
# GCPPreflightChecker verbatim -- no reimplementation of the cloud-side checks.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wizard import (  # noqa: E402
    NEXUS_INFERENCE_MODELS_FILENAME,
    GCPPreflightChecker,
    console,
)

DEFAULT_REGION = "us-central1"
DEFAULT_CIDR = "10.112.0.0/12"

# Reserved ranges the deploy hard-rejects (pulumi_pinecone_byoc/gcp/vpc.py:25-35)
# but the wizard's preflight does NOT check -- mirror them here so preflight
# stays predictive of `pulumi up`.
RESERVED_SUBNETS = ["10.100.1.0/24", "10.100.2.0/24"]  # PSC, regional managed proxy
RESERVED_BLOCK = "10.100.0.0/16"  # broader guard used when suggesting a free range

# The official BYOC docs require roles/owner: "roles/editor is not sufficient
# because BYOC creates IAM service accounts and bindings." These two perms are
# the owner-vs-editor distinguishers (editor can create SAs but cannot set IAM
# policy at the project or SA level). We test the *effective* caller perms via
# the Resource Manager testIamPermissions API, which resolves impersonation and
# group grants -- so it reflects the identity Pulumi actually deploys as (ADC).
OWNER_PERMS = [
    "resourcemanager.projects.setIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
]

# host tools bootstrap.sh requires, plus the GKE auth plugin _validate_gcp_creds
# checks (the one most likely to be missing). (command, label).
HOST_TOOLS = [
    ("uv", "uv"),
    ("pulumi", "Pulumi CLI"),
    ("kubectl", "kubectl"),
    ("gcloud", "gcloud"),
    ("gke-gcloud-auth-plugin", "gke-gcloud-auth-plugin"),
]


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/] {msg}")


def fail(msg: str, hint: str | None = None) -> None:
    console.print(f"  [red]✗[/] {msg}")
    if hint:
        console.print(f"    [dim]{hint}[/]")


def warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/] {msg}")


def section(title: str) -> None:
    console.print()
    console.print(f"  [bold]{title}[/]")
    console.print()


def gcloud_active_project() -> str | None:
    try:
        r = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        value = r.stdout.strip()
        # gcloud prints "(unset)" to stderr/stdout when no project is configured
        return value if value and value != "(unset)" else None
    except Exception:
        return None


def check_host_tools() -> bool:
    section("Host tools")
    all_ok = True
    for cmd, label in HOST_TOOLS:
        if shutil.which(cmd):
            ok(label)
        else:
            all_ok = False
            hint = (
                "gcloud components install gke-gcloud-auth-plugin"
                if cmd == "gke-gcloud-auth-plugin"
                else f"install {label} (see README.md prerequisites)"
            )
            fail(f"{label} not found", hint)
    return all_ok


def check_auth() -> bool:
    section("GCP auth")
    all_ok = True

    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac:
        warn(
            f"GOOGLE_APPLICATION_CREDENTIALS is set ({gac}) — a key-file path "
            "there overrides ADC. Unset it unless that key is intended."
        )

    try:
        r = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            ok("Application Default Credentials are live (Pulumi + wizard use these)")
        else:
            all_ok = False
            fail(
                "ADC did not return a token",
                "gcloud auth application-default login [--impersonate-service-account=<SA>]",
            )
    except Exception as e:
        all_ok = False
        fail(f"ADC check failed: {e}")
    return all_ok


def check_gcp_rapt() -> bool:
    """Verify the GCP RAPT / impersonation session is live by minting a token.

    ADC having a token (check_auth) does not prove the upstream
    reauth-required-for-privileged-access (RAPT) session behind impersonation is
    still valid — that expires separately and only surfaces when Pulumi tries to
    act as the impersonated SA. Mint an impersonated token here so the failure
    shows up in preflight, not mid-`pulumi up`.
    """
    section("GCP impersonation (RAPT session)")
    all_ok = True

    # derive the impersonated SA from ADC when available; otherwise fall back to
    # the known dev SA.
    _, member = _adc_identity()
    if member and member.startswith("serviceAccount:"):
        sa = member.split(":", 1)[1]
    else:
        sa = "silas-nexus-byoc-dev@dev-avi-nexus-byoc.iam.gserviceaccount.com"

    try:
        r = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-access-token",
                f"--impersonate-service-account={sa}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            ok("GCP impersonation token live (RAPT valid)")
        else:
            all_ok = False
            stderr = (r.stderr or "").lower()
            if "reauth" in stderr or "reauthentication" in stderr:
                fail(
                    "GCP RAPT session expired (reauthentication required)",
                    "gcloud auth login --no-launch-browser "
                    "--account=silas@pinecone.io  then  "
                    "gcloud auth application-default login --no-launch-browser "
                    f"--impersonate-service-account={sa}",
                )
            else:
                fail(
                    f"could not mint impersonated token for {sa}",
                    "check the impersonation grant "
                    "(roles/iam.serviceAccountTokenCreator on the SA).",
                )
    except Exception as e:
        all_ok = False
        fail(f"impersonation token check failed: {e}")
    return all_ok


def check_pulumi_session(stack_dir: str | None) -> bool:
    """Verify the Pulumi backend / SSO session is usable for STATEFUL ops.

    `pulumi login` does NOT refresh an expired org SSO (SAML) session; a stale
    session only fails when a stateful op (stack export / up / destroy) runs.
    Probe it here with `stack export` so the SSO reauth need surfaces in
    preflight instead of mid-deploy.
    """
    section("Pulumi backend / SSO session")

    if not stack_dir:
        warn("No --stack-dir given; cannot probe the Pulumi backend session.")
        return True  # nothing to check yet

    try:
        r = subprocess.run(
            ["pulumi", "-C", stack_dir, "stack", "export"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        fail(f"Pulumi stack export failed: {e}")
        return False

    # the real signal is returncode==0 AND non-empty stdout; a fresh stack
    # legitimately has 0 resources, so the count is informational only.
    if r.returncode != 0 or not r.stdout.strip():
        fail(
            "Pulumi backend/SSO session not usable",
            "run SAML SSO reauth (pulumi login won't fix org SSO) at "
            "https://app.pulumi.com/signin/sso/pinecone/reauth, then re-run",
        )
        return False

    try:
        resources = json.loads(r.stdout)["deployment"]["resources"]
        n = len(resources)
    except Exception:
        n = 0
    ok(f"Pulumi backend session live (stack export OK, {n} resources)")
    return True


def _gcloud_account() -> str | None:
    try:
        r = subprocess.run(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        v = r.stdout.strip()
        return v if v and v != "(unset)" else None
    except Exception:
        return None


def _adc_identity() -> tuple[str, str | None]:
    """Resolve the identity ADC (and therefore Pulumi) deploys as.

    Returns (display, iam_member) where iam_member is a binding string suitable
    for `gcloud projects add-iam-policy-binding --member=...` (or None when it
    can't be determined). The ADC file is authoritative: an impersonated-SA
    token doesn't carry a readable email, but the file records the target SA.
    """
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        cfg = os.environ.get("CLOUDSDK_CONFIG") or os.path.join(
            os.path.expanduser("~"), ".config", "gcloud"
        )
        path = os.path.join(cfg, "application_default_credentials.json")

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}

    cred_type = data.get("type")
    if cred_type == "impersonated_service_account":
        url = data.get("service_account_impersonation_url", "")
        m = re.search(r"serviceAccounts/([^:/]+):", url)
        sa = m.group(1) if m else None
        if sa:
            return f"{sa} (impersonated via ADC)", f"serviceAccount:{sa}"
        return "impersonated service account (ADC)", None
    if cred_type == "service_account" and data.get("client_email"):
        email = data["client_email"]
        return f"{email} (service account key)", f"serviceAccount:{email}"
    if cred_type == "authorized_user":
        acct = _gcloud_account()
        if acct:
            return f"{acct} (user credentials)", f"user:{acct}"
        return "user credentials (ADC)", None

    # no/unknown ADC file -- fall back to the active gcloud account
    acct = _gcloud_account()
    return (acct, None) if acct else ("unknown", None)


def _adc_token() -> str | None:
    try:
        r = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def check_iam_owner(project: str) -> bool:
    """Verify the deploy identity has roles/owner-level IAM permissions.

    The wizard never checks this; without it the deploy fails when it tries to
    create IAM service accounts and bindings (docs: roles/editor is insufficient).
    """
    section("GCP IAM (roles/owner)")

    display, member = _adc_identity()
    console.print(f"  [dim]deploy identity (ADC): {display}[/]")
    console.print()

    token = _adc_token()
    if not token:
        warn("Could not get an ADC token to test IAM perms (see the GCP auth check).")
        return True  # don't double-count the auth failure

    body = json.dumps({"permissions": OWNER_PERMS}).encode()
    url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions"
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            granted = set(json.loads(resp.read()).get("permissions", []))
    except Exception as e:
        warn(f"Could not test IAM permissions: {e}")
        return True

    missing = [p for p in OWNER_PERMS if p not in granted]
    if missing:
        fail(
            f"{display} lacks owner-level IAM perms: {', '.join(missing)}",
            "BYOC creates IAM service accounts AND bindings; roles/editor is not enough.",
        )
        if member:
            console.print(
                "    [dim]An existing project owner can grant it:[/]\n"
                f"    [dim]gcloud projects add-iam-policy-binding {project} "
                f'--member="{member}" --role="roles/owner"[/]'
            )
        return False
    ok(f"{display} has owner-level IAM permissions (setIamPolicy)")
    return True


def check_pinecone_api_key() -> bool:
    section("Pinecone API key")
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key:
        fail(
            "PINECONE_API_KEY is not set",
            "export PINECONE_API_KEY=<key> (the wizard validates it over the network)",
        )
        return False

    # same liveness check the wizard runs (_validate_api_key): GET /indexes,
    # treat 401 as an invalid key.
    try:
        req = urllib.request.Request(
            "https://api.pinecone.io/indexes", headers={"Api-Key": api_key}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
        ok("PINECONE_API_KEY is set and valid")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("PINECONE_API_KEY is invalid (401)")
        else:
            fail(f"Pinecone API returned {e.code} while validating the key")
        return False
    except Exception as e:
        fail(f"Could not validate PINECONE_API_KEY: {e}")
        return False


def _in_region_subnet_ranges(project: str, region: str) -> list[ipaddress.IPv4Network]:
    """Primary + secondary subnet ranges in-region, across all networks.

    Used only to compute a suggestion -- the conflict *decision* stays with the
    wizard's checker. Including secondary ranges (GKE pods/services) keeps the
    suggestion clear of ranges a future cluster might auto-allocate.
    """
    ranges: list[ipaddress.IPv4Network] = []
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


def _required_provider_key_refs(stack_dir: str) -> list[str]:
    """api_key_ref values from the project's inference-proxy-models.toml, if any."""
    models_path = os.path.join(stack_dir, NEXUS_INFERENCE_MODELS_FILENAME)
    if not os.path.isfile(models_path):
        return []
    try:
        with open(models_path) as f:
            text = f.read()
    except OSError:
        return []
    return sorted(set(re.findall(r'api_key_ref\s*=\s*"([^"]+)"', text)))


def check_nexus_secrets(stack_dir: str | None) -> bool:
    section("Nexus secrets (the wizard never verifies these)")

    if not stack_dir:
        warn(
            "No --stack-dir given; cannot verify secrets are set. Before `pulumi up` "
            "set, per the generated project:"
        )
        console.print("    [dim]pulumi config set --secret <project>:nexus-gemini-api-key <key>[/]")
        console.print(
            "    [dim]pulumi config set --path --secret "
            "nexus-provider-keys.<api-key-ref> <key>  (one per model api_key_ref)[/]"
        )
        return True  # non-failing: nothing to check yet

    # find the stack config file(s) and check for secret presence by name. Reading
    # the yaml needs no passphrase (secret values are stored encrypted inline).
    stack_files = [
        f
        for f in os.listdir(stack_dir)
        if f.startswith("Pulumi.") and f.endswith(".yaml") and f != "Pulumi.yaml"
    ]
    if not stack_files:
        warn(f"No Pulumi.<stack>.yaml found in {stack_dir} — has the wizard run yet?")
        return True

    blob = ""
    for f in stack_files:
        try:
            with open(os.path.join(stack_dir, f)) as fh:
                blob += fh.read()
        except OSError:
            pass

    all_ok = True
    if "nexus-gemini-api-key" in blob:
        ok("nexus-gemini-api-key is set")
    else:
        all_ok = False
        fail(
            "nexus-gemini-api-key not set",
            "pulumi config set --secret <project>:nexus-gemini-api-key <key>",
        )

    refs = _required_provider_key_refs(stack_dir)
    if not refs:
        warn(
            f"No {NEXUS_INFERENCE_MODELS_FILENAME} found; cannot determine which "
            "nexus-provider-keys refs are required."
        )
    elif "nexus-provider-keys" in blob:
        ok(f"nexus-provider-keys present (model refs: {', '.join(refs)})")
    else:
        all_ok = False
        fail(
            f"nexus-provider-keys not set (models need: {', '.join(refs)})",
            "pulumi config set --path --secret nexus-provider-keys.<ref> <key>",
        )
    return all_ok


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
    results.append(check_gcp_rapt())
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
