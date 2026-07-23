# Pinecone Nexus BYOC

| :warning: WARNING           |
|:----------------------------|
| This repository is under active development and may introduce breaking changes.  |



Deploy Pinecone in your own cloud account with full control over your infrastructure.

> **Supported clouds:** **GCP**, **AWS**, and **Azure** are fully supported
> today — the installer deploys Pinecone Nexus, together with its Pinecone
> Database data plane, into your own cloud account. See [AWS](#aws) for
> AWS-specific operational notes.

![Demo](./assets/demo.gif)

## Quick Start

### 1. Authenticate

The setup script **checks** your credentials but does not log you in, so authenticate
to your cloud and to Pulumi first.

**GCP** (Pulumi deploys using your Application Default Credentials):
```bash
gcloud auth login
gcloud auth application-default login
```

**AWS** — Pulumi deploys using your default AWS credentials:
```bash
aws configure                  # or aws sso login / exported AWS_* env vars
aws sts get-caller-identity    # verify
```

**Azure** — Pulumi deploys using your Azure CLI credentials:
```bash
az login
az account show                # verify
```

**Pulumi** (state backend — Pulumi Cloud, or `pulumi login --local` for local state):
```bash
pulumi login
```

If you use the local backend, choose a passphrase for encrypting stack secrets and
export it as `PULUMI_CONFIG_PASSPHRASE` — every `pulumi` command needs it.

You will also need a **Pinecone API key** (BYOC requires an Enterprise plan) and a
**Gemini API key** — the wizard prompts for both.
Gemini is the shipped default generation LLM; you can point the catalog at other
providers by editing the generated `inference-proxy-models.toml`.

### 2. Clone this repository and run the interactive setup

Run the bootstrap script from a clone (the generated project is created next to it
and depends on it):

```bash
git clone https://github.com/pinecone-io/pulumi-pinecone-nexus-byoc.git
bash pulumi-pinecone-nexus-byoc/bootstrap.sh --cloud gcp
```

Use `--stack-name <name>` to name the Pulumi stack (default: `prod`). Use
`--cloud aws` for an AWS install or `--cloud azure` for an Azure install.

This will:
1. Select your cloud provider (**GCP**, **AWS**, or **Azure**)
2. Check that required tools are installed (Python 3.12+, uv, cloud CLI, Pulumi, kubectl)
3. Verify your cloud credentials
4. Prompt for the project directory and name (press Enter to accept the defaults)
5. Run an interactive setup wizard (collects your project, region, network, and API keys)
6. Generate a complete Pulumi project in an adjacent directory (default:
   `pinecone-nexus-byoc`), wired to your clone of this repository

### 3. Deploy

```bash
cd pinecone-nexus-byoc
pulumi up
```

Provisioning takes approximately 25-30 minutes on GCP, 25-40 minutes on
AWS (see [AWS](#aws)), and roughly 35 minutes on Azure.

### 4. Connect to your cluster

`pulumi up` prints an `update_kubeconfig_command` output — run it to point `kubectl`
at your new cluster. On GCP:

```bash
gcloud container clusters get-credentials <cluster-name> --region <region> --project <project>
kubectl get pods -A
```

(GKE access also requires the `gke-gcloud-auth-plugin` component. See
[Cluster Access](#cluster-access) for the AWS and Azure equivalents.)

The first `pulumi up` also creates a default workspace and prints two more
outputs once it's ready:
- `nexus_default_workspace_data_console_url` — the in-cell workspace console for the `default` workspace.
- `nexus_default_workspace_control_console_url` — the Pinecone Console page for that workspace.

The workspace is named `default` unless you set `nexus-default-workspace-name`.
Workspace names are unique across the whole BYOC project, so when several cells
share one project each additional cell needs a distinct name:

```bash
pulumi config set nexus-default-workspace-name default-<cell-suffix>
```

## Prerequisites

### Accounts & API Keys

| Requirement | Needed for | Notes |
|-------------|-----------|-------|
| **Pinecone API key** | All BYOC | Requires a Pinecone **Enterprise plan** |
| **GCP project** | GCP BYOC | A **dedicated project** with the **Owner** role (`roles/owner`) and **billing enabled** |
| **AWS account** | AWS BYOC | A **dedicated account** with administrator-level access (the deploy creates IAM roles and policies) |
| **Azure subscription** | Azure BYOC | A **dedicated subscription** with **Owner** / administrator-level access |
| **Pulumi account** | All BYOC | A state backend (Pulumi Cloud, or `pulumi login --local` for local state) |
| **Generation-LLM key** (BYOM) | All BYOC | The default catalog's generation LLM (curation + search) is **Gemini** — get a key from [Google AI Studio](https://aistudio.google.com/apikey) and set it as `nexus-provider-keys.gemini-api-key`. The catalog is editable (`inference-proxy-models.toml`): route the chat tiers to other providers, each with its own `nexus-provider-keys.<ref>` secret. Embedding and rerank default to **Pinecone-hosted** models (`multilingual-e5-large` / `bge-reranker-v2-m3`) that need no extra key; the embedding model is catalog-configurable |

> **Nexus generation-LLM capacity:** the generation LLM is the model you bring. The
> shipped default catalog routes all three chat tiers to Gemini (edit
> `inference-proxy-models.toml` to route tiers to other providers). Gemini quota is per
> Google Cloud project and best-effort (no reserved capacity), so a low free-tier project
> will throttle real curation workloads — use a billing-enabled project/tier sized to your
> ingest volume. Create the key in [Google AI Studio](https://aistudio.google.com/apikey);
> associating it with the **same GCP project** as the deployment keeps Gemini
> cost-tracking unified (the key may live in any project, but a shared one consolidates
> billing).

### Common Tools (Required for All Clouds)

| Tool | Purpose | Install |
|------|---------|---------|
| Git | Clone this repository | [git-scm.com](https://git-scm.com/downloads) |
| Python 3.12+ | Runtime | [python.org](https://www.python.org/downloads/) |
| uv | Package manager | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Pulumi | Infrastructure | [pulumi.com/docs/install](https://www.pulumi.com/docs/install/) |
| kubectl | Cluster access | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |

### Cloud-Specific Tools

**GCP** (supported)
| Tool | Purpose | Install |
|------|---------|---------|
| gcloud CLI | GCP access | [GCP docs](https://cloud.google.com/sdk/docs/install) |
| gke-gcloud-auth-plugin | GKE cluster access | `gcloud components install gke-gcloud-auth-plugin` |

**AWS** (supported)
| Tool | Purpose | Install |
|------|---------|---------|
| AWS CLI | AWS access | [AWS docs](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |

**Azure** (supported)
| Tool | Purpose | Install |
|------|---------|---------|
| Azure CLI | Azure access | [Azure docs](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |

## Architecture

```
┌──────────────────────┐                    ┌───────────────────────────────────────────────┐
│                      │    operations      │         Your AWS/GCP/Azure Account (VPC)      │
│  Pinecone            │───────────────────▶│                                               │
│  Control Plane       │                    │  ┌─────────────┐  ┌─────────────────────────┐ │
│                      │◀───────────────────│  │  Control    │  │                         │ │
│                      │   cluster state    │  │  Plane      │  │    Cluster Manager      │ │
└──────────────────────┘                    │  └─────────────┘  │     (EKS/GKE/AKS)       │ │
                                            │  ┌─────────────┐  └─────────────────────────┘ │
                                            │  │  Heartbeat  │                              │
                                            │  └─────────────┘                              │
┌──────────────────────┐                    │  ┌───────────────────────────────────────────┐│
│                      │◀───────────────────│  │                                           ││
│  Pinecone            │   metrics &        │  │              Data Plane                   ││
│  Observability (DD)  │   traces           │  │                                           ││
│                      │                    │  └───────────────────────────────────────────┘│
└──────────────────────┘                    │      ┌──────────┐        ┌─────────────┐      │
                                            │      │ S3/GCS/  │        │ Route53/    │      │
        No customer data                    │      │ AzureBlob│        │ CloudDNS/   │      │
        leaves the cluster                  │      └──────────┘        │ Azure DNS   │      │
                                            │                          └─────────────┘      │
                                            └───────────────────────────────────────────────┘
```

## How It Works

Pinecone BYOC uses a **pull-based model** for control plane operations:

1. **Index Operations** - When you create, scale, or delete indexes through the Pinecone API, these operations are queued in Pinecone's control plane
2. **Pull & Execute** - Components running in your cluster continuously pull pending operations and execute them locally
3. **Heartbeat & State** - Your cluster pushes health status and state back to Pinecone for monitoring
4. **Observability** - Metrics and traces (not customer data) are sent to Pinecone's observability platform (Datadog) for operational insights

This architecture ensures:
- **Your data never leaves your cloud account** - only operational metrics and cluster state are transmitted
- Network security policies remain under your control
- All communication is outbound from your cluster - Pinecone never needs inbound access

## Cluster Access

After deployment, configure kubectl:

**GCP:**
```bash
gcloud container clusters get-credentials <cluster-name> --region <region> --project <project-id>
```

**AWS:**
```bash
aws eks update-kubeconfig --region <region> --name <cluster-name>
```

**Azure:**
```bash
az aks get-credentials --resource-group <resource-group> --name <cluster-name>
```

The exact command is output after `pulumi up` completes.

## Upgrades

Pinecone manages upgrades automatically in the background. A cell is pinned to
two independent image tags that roll separately — `pinecone-version` (the
Pinecone Database images) and `nexus-version` (the Nexus images). If you need to
trigger an upgrade manually, set either pin (or both) and re-run `pulumi up`:

```bash
# Bump the Pinecone Database version
pulumi up -c pinecone-version=<new-db-tag>

# Bump the Nexus version
pulumi up -c nexus-version=<new-nexus-tag>
```

Replace the tags with the target versions (e.g., `main-abc1234`). The two pins
are unrelated — bumping one does not touch the other.

## AWS

On AWS, this repository installs Pinecone Nexus together with its Pinecone
Database data plane. The notes below cover AWS-specific operational behavior.

### AWS install shape

AWS installs run on EKS with a **shared external FoundationDB cluster** serving
both the Pinecone Database data plane and Nexus metadata.
The wizard defaults reflect this shape: `data-plane-backend=fdb`,
`nexus-fdb-mode=external`, and **three availability zones** (fewer than three
silently degrades FDB's zone fault domains).

A cold install runs as a single hands-free `pulumi up`, and `pulumi destroy`
tears the stack back down to zero (with a small manual-cleanup delta documented
under [Teardown notes](#teardown-notes)).

Two install-time knobs to know about:

- **Provider keys on headless installs:** the interactive wizard prompts for the
  default catalog's Gemini key. If you run the wizard non-interactively, set each
  key your catalog references manually before deploying. For the default (Gemini)
  catalog:

  ```bash
  pulumi config set --path --secret nexus-provider-keys.gemini-api-key <key>
  ```

  A customized catalog needs one secret per `api_key_ref` it defines
  (`nexus-provider-keys.<ref>`); the setup preflight lists the refs it expects.

- **Default workspace name:** workspace names are unique across a Pinecone
  project, so when several cells share one project, give each additional cell
  a distinct name via `nexus-default-workspace-name` (see
  [Quick Start](#quick-start)).

**Private-only cells:** on private-only cells (`public_access_enabled = false`),
workspace endpoints over PrivateLink (`*.wksp.private`) are not currently
supported.

### Install expectations

- A cold install takes roughly **25-40 minutes** and provisions ~225 resources
  in a single hands-free `pulumi up`.
- The slowest single step is **VPC endpoint service private DNS verification**:
  about 15 minutes of `Waiting for domain verification (pendingVerification)`
  polling is **normal**, not a hang. Let it finish.

### Updating versions on a live stack

A live cell carries two independent version pins that roll separately — the
Pinecone Database images (`pinecone-version`) and the Nexus images
(`nexus-version`). Bump either one the same way: set the config key, then
`pulumi up`.

```bash
# Pinecone Database images
pulumi config set pinecone-version <db-tag>
pulumi up

# Nexus images
pulumi config set nexus-version <nexus-tag>
pulumi up
```

Bumping `pinecone-version` is a surgical operation: it touches only the pinetools
CronJob, the versioned install Job, and the uninstaller image reference. The
install Job then rolls all DB components to the new tag. Bumping `nexus-version`
is likewise scoped to the Nexus images and their deploy — it does not touch the
DB components.

### Teardown notes

`pulumi destroy` runs the uninstaller Job **before** deleting infrastructure, so
in-cluster workloads — including load balancers created by the
aws-load-balancer-controller — are removed while the controller still exists.
Keep in mind:

- **ACM certificates** are created with `retain_on_delete` by design and survive
  destroy. Delete them manually afterward (`aws acm list-certificates` /
  `aws acm delete-certificate`; they will show `InUse: false`).
- **CloudWatch logs:** EKS leaves a `/aws/eks/<cluster-name>/cluster` log group
  behind. Delete it manually (or set a retention policy) for a truly clean
  account.
- **FDB volumes:** the EBS volumes backing the FoundationDB PersistentVolumeClaims
  can be left behind after destroy.
  Check for orphans (`aws ec2 describe-volumes
  --filters Name=status,Values=available`) and delete them manually.
- **Environment deregistration** happens automatically during destroy — the
  environment resource's delete hook calls the Pinecone control plane. To verify
  the DNS delegation is gone, query the parent zone's authoritative nameservers
  directly; your local resolver will keep serving cached NS records until the
  TTL decays.

## Configuration

The setup wizard creates a Pulumi stack with these configurable options:

**AWS Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `nexus-version` | Nexus release version | — |
| `region` | AWS region | `us-east-1` |
| `availability_zones` | AZs for high availability (3 recommended — FDB zone fault domains) | first 3 available AZs, e.g. `["us-east-1a", "us-east-1b", "us-east-1c"]` |
| `vpc_cidr` | VPC IP range | `10.0.0.0/16` |
| `deletion_protection` | Protect S3 from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = PrivateLink only) | `true` |
| `tags` | Custom tags to apply to all resources | `{}` |

**GCP Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `nexus-version` | Nexus release version | — |
| `gcp_project` | GCP project ID (required) | — |
| `region` | GCP region | `us-central1` |
| `availability_zones` | Zones for high availability (3 recommended — FDB zone fault domains) | first 3 available zones, e.g. `["us-central1-a", "us-central1-b", "us-central1-c"]` |
| `vpc_cidr` | VPC IP range | `10.112.0.0/12` |
| `deletion_protection` | Protect GCS from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = Private Service Connect only) | `true` |
| `labels` | Custom labels to apply to all resources | `{}` |

**Azure Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `nexus-version` | Nexus release version | — |
| `subscription-id` | Azure subscription ID (required) | — |
| `region` | Azure region | `eastus` |
| `availability_zones` | Zones for high availability (3 recommended — FDB zone fault domains) | first 3 available zones, e.g. `["1", "2", "3"]` |
| `vpc_cidr` | VNet IP range | `10.0.0.0/16` |
| `deletion_protection` | Protect databases/storage from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = Private Link only) | `true` |
| `tags` | Custom tags to apply to all resources | `{}` |

Edit `Pulumi.<stack>.yaml` to modify these values.

## Programmatic Usage

For advanced users who want to integrate into existing infrastructure. GCP,
AWS, and Azure are fully supported (the setup wizard generates the project for
you in every case).

```python
import pulumi
from pulumi_pinecone_byoc.aws import PineconeAWSCluster, PineconeAWSClusterArgs

config = pulumi.Config()

cluster = PineconeAWSCluster(
    "pinecone-aws-cluster",
    PineconeAWSClusterArgs(
        pinecone_api_key=config.require_secret("pinecone_api_key"),
        pinecone_version=config.require("pinecone_version"),
        region=config.require("region"),
        availability_zones=config.require_object("availability_zones"),
        vpc_cidr=config.get("vpc_cidr") or "10.0.0.0/16",
        deletion_protection=config.get_bool("deletion_protection") if config.get_bool("deletion_protection") is not None else True,
        public_access_enabled=config.get_bool("public_access_enabled") if config.get_bool("public_access_enabled") is not None else True,
        tags=config.get_object("tags") or {},
    ),
)

# Export useful values
pulumi.export("environment", cluster.environment.env_name)
pulumi.export("cluster_name", cluster.cell_name)
pulumi.export("kubeconfig", cluster.eks.kubeconfig)
```

### Installation

This package is not yet published to PyPI — install it from a clone of this
repository. The setup wizard does this for you: the generated project depends on
your clone via an editable path source. To use it in your own project:

```bash
git clone https://github.com/pinecone-io/pulumi-pinecone-nexus-byoc.git
uv add --editable './pulumi-pinecone-nexus-byoc[gcp]'    # GCP (supported)
uv add --editable './pulumi-pinecone-nexus-byoc[aws]'    # AWS (supported)
uv add --editable './pulumi-pinecone-nexus-byoc[azure]'  # Azure (supported)
```

## Troubleshooting

### Preflight check failures

The setup wizard runs preflight checks for cloud quotas. If these fail:

**AWS:**
1. **VPC Quota** - Request a limit increase via AWS Service Quotas
2. **Elastic IPs** - Release unused EIPs or request a limit increase
3. **NAT Gateways** - Request a limit increase
4. **EKS Clusters** - Request a limit increase

**GCP:**
1. **APIs** - Enable required APIs (compute, container, alloydb, storage, dns)
2. **Compute Quotas** - Request CPU/disk quota increases via GCP Console
3. **GKE Clusters** - Request a limit increase if at quota
4. **IP Addresses** - Release unused static IPs or request more

**Azure:**
1. **Resource Providers** - Register required providers (Microsoft.Compute, Microsoft.ContainerService, etc.)
2. **vCPU Quotas** - Request vCPU quota increases via Azure Portal
3. **AKS Clusters** - Request a limit increase if at quota
4. **Storage Accounts** - Ensure unique naming (3-24 lowercase alphanumeric characters)

### Deployment failures

If `pulumi up` fails partway through:

```bash
pulumi refresh  # Sync state with actual resources
pulumi up       # Retry deployment
```

### Cluster access issues

Ensure your cloud credentials match the account where the cluster is deployed:

```bash
# AWS
aws sts get-caller-identity

# GCP
gcloud auth list
gcloud config get-value project

# Azure
az account show
```

## Cleanup

To destroy all resources:

```bash
pulumi destroy
```

Note: If `deletion_protection` is enabled (default), you'll need to disable it first or manually delete protected resources.

On AWS, read the [teardown notes](#teardown-notes) before running `pulumi destroy`.

## Support

- [Documentation](https://docs.pinecone.io/guides/production/bring-your-own-cloud)
- [GitHub Issues](https://github.com/pinecone-io/pulumi-pinecone-nexus-byoc/issues)
