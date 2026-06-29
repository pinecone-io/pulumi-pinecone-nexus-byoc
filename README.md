# Pinecone BYOC

[![PyPI version](https://img.shields.io/pypi/v/pulumi-pinecone-byoc)](https://pypi.org/project/pulumi-pinecone-byoc/)

Deploy Pinecone in your own cloud account with full control over your infrastructure.

> **Supported clouds:** **GCP** is supported today. **AWS** and **Azure** are
> **coming soon** — the wizard and docs may reference them, but they are not yet
> supported for production deployments.

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

_AWS and Azure authentication: coming soon (not yet supported)._

**Pulumi** (state backend — Pulumi Cloud, or `pulumi login --local` for local state):
```bash
pulumi login
```

You will also need a **Pinecone API key** (BYOC requires an Enterprise plan). If you
enable **Nexus**, have a **Gemini API key** ready as well — the wizard prompts for both.

### 2. Run the interactive setup

```bash
curl -fsSL https://raw.githubusercontent.com/pinecone-io/pulumi-pinecone-byoc/main/bootstrap.sh | bash
```

This will:
1. Select your cloud provider (**GCP** — AWS and Azure coming soon)
2. Check that required tools are installed (Python 3.12+, uv, cloud CLI, Pulumi, kubectl)
3. Verify your cloud credentials
4. Run an interactive setup wizard (collects your project, region, network, and API keys)
5. Generate a complete Pulumi project

### 3. Deploy

```bash
cd pinecone-byoc
pulumi up
```

Provisioning takes approximately 25-30 minutes.

### 4. Connect to your cluster

`pulumi up` prints an `update_kubeconfig_command` output — run it to point `kubectl`
at your new cluster. On GCP:

```bash
gcloud container clusters get-credentials <cluster-name> --region <region> --project <project>
kubectl get pods -A
```

(GKE access also requires the `gke-gcloud-auth-plugin` component. AWS and Azure
support is coming soon.)

## Prerequisites

### Accounts & API Keys

| Requirement | Needed for | Notes |
|-------------|-----------|-------|
| **Pinecone API key** | All BYOC | Requires a Pinecone **Enterprise plan** |
| **Cloud account** (GCP) | All BYOC | A GCP project with owner/billing — the setup wizard checks for `roles/owner` |
| **Pulumi account** | All BYOC | A state backend (Pulumi Cloud, or `pulumi login --local` for local state) |
| **Gemini API key** (BYOM) | **Nexus only** | The bring-your-own **generation LLM** (curation + search). Embedding (`multilingual-e5-large`) and rerank (`bge-reranker-v2-m3`) are **Pinecone-hosted** — no extra key needed |

> **Nexus model capacity:** Gemini is the only model you bring. Its quota is per Google
> Cloud project and best-effort (no reserved capacity), so a low free-tier project will
> throttle real curation workloads — use a billing-enabled project/tier sized to your
> ingest volume.

### Common Tools (Required for All Clouds)

| Tool | Purpose | Install |
|------|---------|---------|
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

**AWS** _(coming soon)_ · **Azure** _(coming soon)_

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
└──────────────────────┘                    │  ┌──────────┐  ┌───────────┐  ┌─────────────┐ │
                                            │  │ S3/GCS/  │  |RDS/AlloyDB|  │ Route53/    │ │
        No customer data                    │  │ AzureBlob│  │/AzurePGSQL|  | CloudDNS/   | │
        leaves the cluster                  │  └──────────┘  └───────────┘  | Azure DNS   | │
                                            │                               └─────────────┘ │
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

**AWS** _(coming soon)_:
```bash
aws eks update-kubeconfig --region <region> --name <cluster-name>
```

**Azure** _(coming soon)_:
```bash
az aks get-credentials --resource-group <resource-group> --name <cluster-name>
```

The exact command is output after `pulumi up` completes.

## Upgrades

Pinecone manages upgrades automatically in the background. If you need to trigger an upgrade manually:

```bash
pulumi up -c pinecone-version=<new-version>
```

Replace `<new-version>` with the target Pinecone version (e.g., `main-abc1234`).

## Configuration

The setup wizard creates a Pulumi stack with these configurable options:

**AWS Configuration Options** _(coming soon — not yet supported)_**:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `region` | AWS region | `us-east-1` |
| `availability_zones` | AZs for high availability | `["us-east-1a", "us-east-1b"]` |
| `vpc_cidr` | VPC IP range | `10.0.0.0/16` |
| `deletion_protection` | Protect RDS/S3 from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = PrivateLink only) | `true` |
| `tags` | Custom tags to apply to all resources | `{}` |

**GCP Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `gcp_project` | GCP project ID (required) | — |
| `region` | GCP region | `us-central1` |
| `availability_zones` | Zones for high availability | `["us-central1-a", "us-central1-b"]` |
| `vpc_cidr` | VPC IP range | `10.112.0.0/12` |
| `deletion_protection` | Protect AlloyDB/GCS from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = Private Service Connect only) | `true` |
| `labels` | Custom labels to apply to all resources | `{}` |

**Azure Configuration Options** _(coming soon — not yet supported)_**:**

| Option | Description | Default |
|--------|-------------|---------|
| `pinecone-version` | Pinecone release version (required) | — |
| `subscription-id` | Azure subscription ID (required) | — |
| `region` | Azure region | `eastus` |
| `availability_zones` | Zones for high availability | `["1", "2"]` |
| `vpc_cidr` | VNet IP range | `10.0.0.0/16` |
| `deletion_protection` | Protect databases/storage from accidental deletion | `true` |
| `public_access_enabled` | Enable public endpoint (false = Private Link only) | `true` |
| `tags` | Custom tags to apply to all resources | `{}` |

Edit `Pulumi.<stack>.yaml` to modify these values.

## Programmatic Usage

For advanced users who want to integrate into existing infrastructure. GCP is the
supported cloud today (the setup wizard generates the project for you); the AWS
example below is illustrative — AWS and Azure are coming soon.

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

Install from PyPI with cloud-specific dependencies:

```bash
# GCP (supported)
uv add 'pulumi-pinecone-byoc[gcp]'

# AWS and Azure — coming soon (not yet supported)
# uv add 'pulumi-pinecone-byoc[aws]'
# uv add 'pulumi-pinecone-byoc[azure]'
```

## Troubleshooting

### Preflight check failures

The setup wizard runs preflight checks for cloud quotas. If these fail:

**AWS** _(coming soon)_:
1. **VPC Quota** - Request a limit increase via AWS Service Quotas
2. **Elastic IPs** - Release unused EIPs or request a limit increase
3. **NAT Gateways** - Request a limit increase
4. **EKS Clusters** - Request a limit increase

**GCP:**
1. **APIs** - Enable required APIs (compute, container, alloydb, storage, dns)
2. **Compute Quotas** - Request CPU/disk quota increases via GCP Console
3. **GKE Clusters** - Request a limit increase if at quota
4. **IP Addresses** - Release unused static IPs or request more

**Azure** _(coming soon)_:
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

## Support

- [Documentation](https://docs.pinecone.io/guides/production/bring-your-own-cloud)
- [GitHub Issues](https://github.com/pinecone-io/pulumi-pinecone-byoc/issues)
