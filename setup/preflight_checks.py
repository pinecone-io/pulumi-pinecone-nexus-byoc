#!/usr/bin/env python3
"""Shared BYOC preflight checks — single source of truth.

These are the UNIQUE up-front checks the standalone `dev/preflight.py` adds on
top of the wizard's cloud-side `GCPPreflightChecker` (quotas / APIs / CIDR). They
live here, separate from both, so the wizard and the standalone tool call ONE
implementation instead of duplicating it:

  - host tools + live auth (otherwise only checked inside bootstrap.sh /
    _validate_gcp_creds),
  - GCP ADC liveness, Pulumi backend/SSO session, Pinecone API key liveness,
    IAM roles/owner perms, and
  - Nexus provider secrets (the wizard only PRINTS reminders for these, so a
    missing key surfaces at `pulumi up` instead of here).

Each check is a small function returning pass/fail and printing rich output via
the shared `console`. This module imports NOTHING from `wizard` so it can be
imported from both `wizard` and `preflight` without a cycle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from rich.console import Console

console = Console()

# Models file the generated project carries; the api_key_ref values inside it
# tell us which nexus-provider-keys secrets must be set before `pulumi up`.
NEXUS_INFERENCE_MODELS_FILENAME = "inference-proxy-models.toml"

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


def adc_is_impersonated() -> bool:
    """True when ADC is configured to impersonate a service account.

    Used to auto-enable the RAPT check for impersonation-based ADC without an
    explicit opt-in flag.
    """
    cred_type, _ = _adc_cred()
    return cred_type == "impersonated_service_account"


def check_gcp_rapt(service_account: str | None = None) -> bool:
    """Verify the GCP RAPT / impersonation session is live by minting a token.

    OFF by default: only meaningful when the deploy identity impersonates a
    service account. Pass an explicit ``service_account`` (the opt-in), or rely
    on the auto-detection in the callers when ADC is already impersonation-based
    (in which case the SA is derived from ADC).

    ADC having a token (check_auth) does not prove the upstream
    reauth-required-for-privileged-access (RAPT) session behind impersonation is
    still valid — that expires separately and only surfaces when Pulumi tries to
    act as the impersonated SA. Mint an impersonated token here so the failure
    shows up in preflight, not mid-`pulumi up`.
    """
    section("GCP impersonation (RAPT session)")

    # prefer the explicit opt-in SA; otherwise derive it from impersonation-based
    # ADC. Never inject a fallback SA -- skip instead.
    sa = service_account
    if not sa:
        _, member = _adc_identity()
        if member and member.startswith("serviceAccount:"):
            sa = member.split(":", 1)[1]
    if not sa:
        warn(
            "No impersonation service account (ADC is not impersonation-based and "
            "--impersonate-service-account was not given); skipping RAPT check."
        )
        return True

    all_ok = True
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
                    "--account=<your-account>  then  "
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
        warn("No stack dir given; cannot probe the Pulumi backend session.")
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
            "your org's SSO session may have expired (pulumi login won't refresh "
            "org SSO) — re-run SAML SSO reauth for your Pulumi org, then re-run",
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


def _adc_cred() -> tuple[str | None, dict]:
    """Read the ADC file and return (credential type, raw data)."""
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
    return data.get("type"), data


def _adc_identity() -> tuple[str, str | None]:
    """Resolve the identity ADC (and therefore Pulumi) deploys as.

    Returns (display, iam_member) where iam_member is a binding string suitable
    for `gcloud projects add-iam-policy-binding --member=...` (or None when it
    can't be determined). The ADC file is authoritative: an impersonated-SA
    token doesn't carry a readable email, but the file records the target SA.
    """
    cred_type, data = _adc_cred()

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

    The wizard's cloud-side checker never tests this; without it the deploy
    fails when it tries to create IAM service accounts and bindings (docs:
    roles/editor is insufficient).
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


def check_pinecone_api_key(api_key: str | None = None) -> bool:
    """Validate the Pinecone API key over the network (GET /indexes; 401 = bad).

    Accepts an explicit key (e.g. one the wizard just collected); falls back to
    PINECONE_API_KEY from the environment.
    """
    section("Pinecone API key")
    api_key = api_key or os.environ.get("PINECONE_API_KEY")
    if not api_key:
        fail(
            "PINECONE_API_KEY is not set",
            "export PINECONE_API_KEY=<key> (the wizard validates it over the network)",
        )
        return False

    try:
        req = urllib.request.Request(
            "https://api.pinecone.io/indexes", headers={"Api-Key": api_key}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
        ok("Pinecone API key is set and valid")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("Pinecone API key is invalid (401)")
        else:
            fail(f"Pinecone API returned {e.code} while validating the key")
        return False
    except Exception as e:
        fail(f"Could not validate Pinecone API key: {e}")
        return False


def required_provider_key_refs(stack_dir: str) -> list[str]:
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
            "No stack dir given; cannot verify secrets are set. Before `pulumi up` "
            "set, per the generated project:"
        )
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
    refs = required_provider_key_refs(stack_dir)
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
