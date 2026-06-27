# BYOC Preflight (`setup/preflight.py`)

On-demand prerequisite checks for a Nexus + DB BYOC install iteration. It runs
the checks the wizard would run — *before* you commit to a 25–30 min deploy —
plus the few the wizard doesn't (live auth, `roles/owner`, Nexus secrets).

It is a thin wrapper: the GCP cloud-side checks delegate to the wizard's own
`GCPPreflightChecker`, so they can't drift from the real install path. It only
adds the judgment layer (an identity readout, the deploy-time IAM/CIDR rules the
wizard skips, and a free-CIDR suggestion). **Read-only** — it never mutates
config or cloud state.

## Run it

Iterations run from the workspace parent (`workspace-coordination/`, which holds
all three repos), so paths below are relative to that. From inside this repo,
drop the `pulumi-pinecone-nexus-byoc/` prefix.

```bash
# --no-project avoids building the repo's own pulumi package.
uv run --no-project --with rich --with pyyaml \
  python pulumi-pinecone-nexus-byoc/setup/preflight.py
```

The script resolves its `wizard` import from its own directory, so it works from
any cwd. First run builds a small uv env (~15s); subsequent runs are a few
seconds. Exit code is `0` only when every hard check passes — gate your loop on it.

With a generated project, also verify the Nexus provider secrets are set
(`--stack-dir` points at wherever the wizard wrote the project, e.g. `pinecone-byoc`
in the parent):

```bash
uv run --no-project --with rich --with pyyaml \
  python pulumi-pinecone-nexus-byoc/setup/preflight.py --nexus --stack-dir pinecone-byoc
```

### Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--project` | active gcloud config | GCP project id to check against |
| `--region` | `us-central1` | region for quota/zone/subnet checks |
| `--zones` | `<region>-a,<region>-b` | zones to validate machine-type availability |
| `--cidr` | `10.112.0.0/12` | candidate VPC CIDR to test for conflicts |
| `--nexus` | off | also check Nexus provider secrets |
| `--stack-dir DIR` | — | generated project dir to verify secrets against (implies `--nexus`) |

## What it checks

1. **Host tools** — `uv`, `pulumi`, `kubectl`, `gcloud`, `gke-gcloud-auth-plugin`.
2. **GCP auth** — ADC returns a token; warns if `GOOGLE_APPLICATION_CREDENTIALS`
   is set (a key-file there overrides ADC).
3. **GCP IAM (`roles/owner`)** — prints the identity ADC deploys as (e.g. the
   impersonated SA) and tests the effective `setIamPolicy` permissions. BYOC
   creates IAM service accounts *and* bindings, so `roles/editor` is not enough.
4. **Pinecone API key** — `PINECONE_API_KEY` set and valid (live `GET /indexes`).
5. **GCP cloud-side** — the wizard's checker: required APIs, VPC/IP/GKE quotas,
   machine types, zones, CIDR conflicts. Plus the reserved-range guard
   (`10.100.0.0/16`) the deploy enforces but the wizard preflight omits, and a
   free-`/12` suggestion when the CIDR conflicts.
6. **Nexus secrets** (`--nexus`) — `nexus-gemini-api-key` + each
   `nexus-provider-keys.<ref>` the model catalog requires. The wizard only
   *prints reminders* for these; missing keys otherwise fail at `pulumi up`.

It also prints the prereqs it *can't* verify: a Pinecone **Enterprise plan**, and
the "open a new terminal after installing a tool so PATH updates" reminder.

## Common failures → fixes

| Failure | Fix |
|---------|-----|
| `VPC CIDR ... conflicts` | Use the suggested `--cidr` (e.g. another dev already holds `10.112.0.0/12`); set it via `pulumi config set <project>:vpc-cidr <cidr>`. |
| `... lacks owner-level IAM perms` | Have a project owner run the printed `add-iam-policy-binding ... --role=roles/owner`, or point ADC at an owner identity. |
| `Pulumi CLI not found` | Install it, then **open a new shell** (PATH). |
| `PINECONE_API_KEY is not set` | `export PINECONE_API_KEY=<key>`. |
| `nexus-gemini-api-key not set` | `pulumi config set --secret <project>:nexus-gemini-api-key <key>` in the generated project. |

## Where it fits

Step 1 of each iteration — see the iteration handoff
([`_plans/2026-06-nexus-byoc/nexus-byoc-iteration-handoff.md`](../_plans/2026-06-nexus-byoc/nexus-byoc-iteration-handoff.md))
for the full preflight → wizard → deploy → verify → fix → reset loop.
