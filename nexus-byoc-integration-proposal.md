# Nexus + Pinecone DB BYOC — Integration Proposal

> Status: scoped for PoC. Goal: a single BYOC deployment that ships **Nexus**
> as the product with **Pinecone DB embedded as an internal dependency**. Nexus's
> **vector ops** run against the in-cluster DB data plane; **index CRUD and inference
> stay on managed `api.pinecone.io`** (pull-based control plane; external inference
> cluster) — see Locked decisions below.
>
> Context source-of-truth: `../pinecone-db/Nexus-BYOC-Decision-Memo.md`,
> `nexus-byoc-design.md`, `nexus-byoc-requirements.md`.
>
> **Implementation branches** (PoC, Phases 1+2 landed, gated off by default):
> | Repo | Branch |
> |------|--------|
> | `pulumi-pinecone-byoc` (this) | `avi/nexus-poc` |
> | `nexus` | `avi/byoc-integration` |
> | `pinecone-db` | `avi/nexus/poc` (context/source-of-truth only; no code changes) |

## 0. Locked decisions (PoC)

Settled in design review. These govern the PoC; the rows tagged _(later)_ note the
deferred end-state.

| # | Decision | Notes |
|---|----------|-------|
| **Packaging** | **Path A** — Nexus stays its own repo; this repo adds a `Nexus` Pulumi component. **Not** a submodule in `pinecone-db`. | Coordinated versions via a combined manifest (`pinecone-version` + `nexus-version`). |
| **Deploy shape** | **One combined `pulumi up`** brings up DB **and** Nexus in a single cluster; add `nexus-role: services` / `jobs` node pools to the existing cluster. | |
| **Cloud** | **GCP for the PoC**; **Azure is the first real target** → parameterize cloud/region + BYOC `deployment.environment` id, no GCP-hardcoding. | Rip out hardcoded `gcp`/`us-central1` in index creation. |
| **Control-plane path** | Index CRUD → **managed `api.pinecone.io`** (pull-based, scoped to the BYOC env). Only **vector ops** are in-cluster. | Index create is per-context (not per build run), so control-plane ops stay low. |
| **Index / data-plane API** | Move Nexus to the BYOC **schema/document model**: `deployment_type: byoc`, schema vector field + FTS string field, **Dedicated** read-capacity, **documents API** (`/namespaces/{ns}/documents/upsert\|search`). In scope. | Replaces today's `create-for-model` + `/vectors/upsert` + `/query`. |
| **Index topology** | **One schema index per context**, pre-provisioned via managed control plane at context-creation. **Fold keyword (FTS) into that same index** (dense field + FTS field). | Hybrid = two searches (dense, FTS) merged — one scoring method per request. _(later: split if needed.)_ |
| **Namespaces (D-NS)** | **Single namespace per index for the PoC** (Dedicated indexes allow only one populated namespace). Collapse today's `default` + `artifacts` (and ephemeral build runs) into one namespace, **partitioned by the existing `kind` metadata field**. | Drops the ephemeral-namespace lever; build runs isolate via a metadata tag, cleaned up by filter. _(later: multi-namespace via control-plane allowlist.)_ |
| **Inference** | **No in-cluster inference exists.** All **embed + rerank** go to **managed `api.pinecone.io/embed` + `/rerank`** → the external inference cluster, using the deployment key. | Embedding becomes an **explicit `/embed` call returning vectors**, upserted via the documents API (integrated server-side path is gone). External is fine for PoC. |
| **Inference seam** | Keep all inference behind a **thin client module** in `runtime/skills` (env-configured endpoint+key), used for embed/rerank/LLM. | _(later: a real in-cluster Inference Proxy PR centralizes egress/creds/metering and makes it boundary-compliant — supersedes this seam.)_ |
| **Credential** | **One deployment-minted Pinecone API key** (reuse the existing `ApiKey`/`__SLI__` control-plane pattern), handed to Nexus as `PINECONE_API_KEY`, shared across the deployment. | _(later: per-user/per-context scoped keys.)_ |
| **Nexus user auth** | **Keep existing login/JWT.** At login, source the deployment key instead of a user-supplied one; `pinecone_key` claim is populated from it (vestigial). | _(later: SAML/OIDC, real multi-user.)_ |
| **Ingress** | **Nexus `gateway` is the customer front door** via the existing ingress (public/PrivateLink). DB data plane stays **internal-only**, not exposed. | |
| **Data boundary** | Text leaving the cluster for external inference is **accepted for the PoC**. | _(later: in-boundary / BYOM inference — lands independently via the proxy PR.)_ |

**Facts now verified against pinecone-db** (D-EP reachable in-cluster, D-FTS supported,
D-REG host-wide/IAM-gated, D-CAP `b1`/1/1, D-NS resolved → single namespace) — details in §5.
No open blockers; remaining work is implementation.

## 1. Where things stand today

Three moving parts:

| Repo | What it does today | Relationship to the goal |
|------|--------------------|--------------------------|
| **pulumi-pinecone-byoc** (this repo) | Pulumi IaC that stands up Pinecone **DB** in a customer's AWS/GCP/Azure account: VPC, K8s (EKS/GKE/AKS), Postgres, object storage, DNS, NLB/ILB, addons, Pulumi operator, secrets/configmaps. DB platform is installed into `pc-control-plane` via the `Pinetools` job (`pinetools cluster install`) at a pinned `pinecone-version`. Pull-based control plane (CPGW key, heartbeats). | Already deploys the DB half of the target. **Does not deploy Nexus at all** (no `nexus` references in `pulumi_pinecone_byoc/`). |
| **nexus** (vendored at `./nexus`, also `pinecone-io/nexus`) | Rust services (api, orchestrator, file-proxy, admin, metrics-collector) + Python `knowql` + Python `runtime` + `console` + nginx `gateway`, backed by **its own in-cluster FoundationDB** (`b"nx"` keyspace). Today deployed to a **managed GKE** cluster (`nexus.pinecone.io`) via `deploy/helm/{nexus,nexus-fdb}` + `deploy/pulumi/{nexus-alpha-k8s,nexus-lb}`. | This is the product to deploy into the BYOC cluster. |
| **pinecone-db** (`../pinecone-db`) | The DB platform whose images `Pinetools` installs. | Source of the in-cluster **data plane** Nexus's vector ops hit. Control plane stays managed (pull-based); inference is **external** (no in-cluster inference exists). |

### How Nexus talks to Pinecone DB today

Nexus issues **two** kinds of Pinecone traffic, both currently pointed at managed SaaS:

1. **Control-plane** (index create/delete/list, project resolve). Hardcoded to
   `https://api.pinecone.io` with a preprod header `x-environment: preprod-aws-0`:
   - Rust `PineconeClient::new()` — `common/src/pinecone.rs:19,72`; call sites:
     `api/src/rest/handlers/auth.rs:43`, `api/src/rest/handlers/contexts.rs:783,1048`,
     `orchestrator/src/task_manager.rs:91`. (`with_base_url` exists but is only used in tests.)
   - Python `runtime/optimize.py:87` — `PINECONE_API_BASE = "https://api.pinecone.io"`.
2. **Data-plane** (upsert/query/rerank), from the Python `runtime` task pods, against the
   per-index `host` the control plane returns (`runtime/skills/pinecone/pinecone.py`).

### The finding that drives the hard part: Nexus is coupled to **integrated inference**

Nexus does **not** do client-side embedding. It relies on Pinecone's integrated inference:

- Indexes are created with `POST /indexes/create-for-model`, embedding model
  **`multilingual-e5-large`**, `field_map: {text: chunk_text}` (`runtime/optimize.py:255`).
- Upserts go through the **integrated records API** — records carry text, embedded
  **server-side** (`runtime/skills/pinecone/pinecone.py:733`, `:1559`).
- Reranking uses Pinecone **integrated rerank** (`pinecone-rerank-v0`) server-side.
- Index creation also hardcodes `"cloud": "gcp", "region": "us-central1"` and uses the
  alpha FTS API (`2026-01.alpha`) for keyword/BM25 indexes.

So "route Nexus's DB requests to the in-cluster DB" is necessary but **not sufficient**:
something must still serve embedding + rerank, and the BYOC index/data-plane API differs
from what Nexus uses today. **All of that is settled in §0** — for the PoC: embed/rerank go
to managed inference behind a thin client lib; the data path moves to the schema/document
model + documents API with explicit embedding. The rest of this doc (§3 onward) is the
implementation of those decisions; §8–§11 are the agent-executable breakdown.

## 2. Target architecture

One BYOC cluster, two stacks, DB not customer-facing:

```
   Customer cloud account / VPC ── one K8s cluster
   ┌──────────────────────────────────────────────────────────────┐
   │  ingress (NLB/ILB) ──► Nexus gateway ──► console + nexus-api   │  ◄── customer front door
   │                                   │                           │
   │   Nexus services (services pool)  │   Nexus task pods (jobs)  │
   │     api / orchestrator / knowql   │     runtime: curate /     │
   │     file-proxy / console / gw     │     optimize / query      │
   │            │                      │            │              │
   │            └── Nexus FDB (b"nx")   │            │              │
   │                                   ▼            ▼              │
   │            Pinecone DB DATA PLANE (in-cluster) ◄── vector ops   │  ◄── embedded dependency,
   │              query-router / documents API                       │      NOT exposed to customer
   │              + pc-fdb + Postgres + object storage               │
   └──────────────────────────────────────────────────────────────┘
        outbound (managed api.pinecone.io) ──► index CRUD (pull-based control plane)
                                          ──► embed + rerank (→ external inference cluster)
        outbound ──► Datadog (metrics)
```

Key properties (PoC):
- **Index CRUD and inference (`/embed`, `/rerank`) go to managed `api.pinecone.io`** with the
  deployment key; only **vector ops** (documents upsert/search) hit the in-cluster data plane,
  via the per-index `host` the managed control plane returns (must be in-cluster reachable).
- The customer sees **Nexus** (gateway front door); the DB data plane is **internal only**.
- A real in-cluster **Inference Proxy** (centralized egress/creds/metering, boundary-compliant)
  is the deferred end-state — see §0; for the PoC inference is a thin client lib calling managed.
- Nexus reuses the cluster's Datadog agent DaemonSet (Nexus chart already follows the `pc-o11y` `DD_TAGS`/`DD_AGENT_HOST` convention).

## 3. Changes in **nexus** (make DB target configurable + embedded-dependency auth)

1. **BYOC index creation (schema/document model).** Index CRUD still targets managed
   `api.pinecone.io`, but the request shape changes: `deployment_type: byoc` + the BYOC
   `deployment.environment` id, schema with a `dense_vector` field **and** an FTS string
   field, **Dedicated** read-capacity. Replaces today's `create-for-model`. Drop the
   hardcoded `gcp`/`us-central1` — cloud/region/env come from deploy config (GCP now,
   Azure later). Affects Rust `common/src/pinecone.rs` (+ `api` call sites) and
   `runtime/optimize.py`. **One schema index per context.**
2. **Data path → documents API + explicit embed.** Move vector ops off `/vectors/upsert`
   + `/query` to `/namespaces/{ns}/documents/upsert|search` against the in-cluster `host`.
   Since integrated server-side embedding is gone, the runtime first calls managed
   `/embed` to get vectors, then upserts them. Hybrid = dense search + FTS search merged
   (one scoring method per request). **Single namespace per index** (D-NS) — `default` +
   `artifacts` collapse into one namespace partitioned by the `kind` field; build runs
   isolate via a build-id tag. Touches `runtime/skills/pinecone/pinecone.py` +
   `curate`/`query`/`optimize`.
3. **Single deployment credential.** Drop the user-supplied DB key. At login, source the
   deployment-minted key (`PINECONE_API_KEY`, already plumbed end-to-end via
   `common/src/config.rs`) and keep populating the `pinecone_key` JWT claim from it so the
   downstream chain (`contexts` → `tasks` → orchestrator → pods) is untouched. Keep
   existing login/JWT otherwise.
4. **Inference client lib.** Add a thin `runtime/skills/inference/` module (env-configured
   endpoint + key) used for embed + rerank + LLM, calling managed `api.pinecone.io/embed`
   + `/rerank` for now. Keep all inference behind this one seam so the future Inference
   Proxy PR is a one-module endpoint swap.
5. **Helm surface.** Add to `deploy/helm/nexus/values.yaml`: the DB endpoint/env config,
   the inference endpoint+key, and the deployment Pinecone key (as a secret). Thread into
   api/orchestrator/knowql env and into the task-pod env the orchestrator builds.

Defaults keep the existing managed Nexus deploy working (changes gate on BYOC config).

## 4. Changes in **pulumi-pinecone-byoc** (deploy Nexus alongside DB)

1. **New `Nexus` component** (parallel to `Pinetools`) that, after the DB stack is up:
   - Installs `nexus-fdb` then `nexus` Helm releases (charts vendored at
     `nexus/deploy/helm`). Either `pulumi_kubernetes.helm.v3.Release` or a
     Pinetools-style installer Job.
   - Sets the managed control-plane base + BYOC `deployment.environment` id; vector ops
     auto-follow the in-cluster `host` the control plane returns.
   - Sets `image.registry` to the BYOC registry (`common/registry.py` `GCP_REGISTRY`) and
     the `nexus-version` tag, plus `image.pullSecrets: [regcred]` (see item 2).
   - Injects the minted deployment Pinecone key into the nexus config secret
     (extend `common/k8s_secrets.py`), plus JWT secret and inference endpoint+key.
2. **Image registry wiring.** Nexus images must live in the BYOC-distributed Pinecone
   registry and be pullable via the brokered `regcred` secret. Two parts: **(a) publish** —
   nexus CI pushes `nexus_*` images to that registry under `nexus-version`; **(b) pull
   auth** — `regcred` is created today by `RegistryCredentialRefresher` only in `pc-*` (+ a
   few extra) namespaces, so extend its `EXTRA_NAMESPACES` to cover `nexus` and
   `nexus-tasks` (or run Nexus in a covered namespace). Confirm the `/cr-token` gcr scope
   covers the Nexus image paths. See answer below — this is the open piece.
3. **Node pools.** The nexus chart schedules onto `nexus-role: services` and
   `nexus-role: jobs` (nodeSelector + tolerations). Add these pools to the
   EKS/GKE/AKS node-pool definitions (today only DB pools exist).
4. **Storage.** Nexus needs a storage class for its PVCs (FDB data, tasks, source,
   knowledge, contexts) — and ideally RWX (Filestore/EFS) for rolling updates. Wire
   `persistence.storageClass` / NFS from the cloud stack.
5. **Ingress/routing.** Expose the Nexus `gateway` through the existing NLB/ILB (the
   customer front door). Keep the DB data plane internal.
6. **Wizard + config.** Add Nexus enablement + inference config to `setup/wizard.py`
   and `config/{base,aws,gcp,azure}.py`. For a "Nexus BYOC" install, provision both and
   suppress DB-level customer surfaces (per the decision memo).
7. **Versioning.** Add a `nexus-version` alongside `pinecone-version`, ideally a single
   combined manifest so DB + Nexus images are upgraded as a coordinated release.

## 5. Inference (PoC) and the proxy end-state

**PoC (settled — see §0):** there is **no in-cluster inference**. Embed + rerank go to
managed `api.pinecone.io/embed` + `/rerank` (→ the external inference cluster), LLM to its
existing external endpoints — all behind a thin `runtime/skills/inference/` client module
(env-configured endpoint + key). Embedding becomes an explicit `/embed` call returning
vectors that get upserted via the documents API. Text leaving the cluster for inference is
accepted for the PoC.

**End-state (deferred, lands independently):** a real in-cluster **Inference Proxy** as the
only inference egress — pluggable backend (BYOM / in-cluster vLLM/TGI), scoped credentials
minted per deployment, per-tenant metering, and the single enforcement point for the
data-boundary / external-access policy (`nexus-byoc-requirements.md` §1,5,6; design D6/D7).
The thin client module is the seam: the proxy PR is a one-module endpoint swap.

### Verified against pinecone-db (facts now closed)

- **D-EP — Index `host` reachability → RESOLVED (in-cluster reachable).** The host
  `{index}-{vault}.svc.{env}.pinecone.io` matches a `*.svc` wildcard the **cluster's own**
  Cloud DNS zone answers (CPGW delegates `{subdomain}.byoc.pinecone.io` to the cluster's
  nameservers), terminating at the in-cluster `gateway-proxy` Service via the internal LB
  (`pc-commons/.../index_metadata_store/global.rs:1271`; `environment_store/mod.rs:230-265`
  derives a `.svc.private.` host when public endpoint is off; `gcp/nlb.py:176-241`,
  `gcp/dns.py`, `common/naming.py` `DNS_CNAMES=["*.svc",…]`). **Action:** use the
  `.svc.private.` host and allow pod→internal-LB egress in NetworkPolicy.
- **D-FTS — FTS field → RESOLVED (supported for BYOC).** FTS string fields are rejected
  only for `deployment_type: pod`; Serverless/BYOC are allowed, and dense_vector + FTS
  string can coexist in one index (`svc-global-apis/.../index/validate.rs:129-161`;
  `…/global_index_v2/mod.rs:387-423`). Caveat: **CMEK + FTS are mutually exclusive**.
- **D-REG — Registry token → RESOLVED (host/project-wide, IAM-gated).** The `/cr-token`
  handler is in-repo (`svc-global-apis/.../cr_token.rs`) and returns a GCP token with
  `cloud-platform` scope for host `us-docker.pkg.dev` — **not** repo-scoped, so `nexus_*`
  is not denied at the token level. Real gate: the token SA's IAM + publishing Nexus
  images under `us-docker.pkg.dev/pinecone-artifacts` (DB images use the `.../unstable`
  repo). **Action:** publish `nexus_*` to a `pinecone-artifacts` repo the SA can read.
- **D-CAP — Dedicated sizing → RESOLVED.** Two node types: `b1` (default, smaller) and
  `t1` (larger, in-memory projections). Validation: `shards >= 1`, `replicas >= 0`.
  **Minimal PoC: `b1`, shards 1, replicas 1.** Note: Dedicated capacity is LD-flagged
  (`ALLOW_DEDICATED_CAPACITY_USAGE`) for non-Internal orgs — Internal/PoC orgs bypass; an
  Enterprise customer org needs the flag on.

_(D-CRED resolved earlier: reuse `ApiKey`/`__SLI__` minting. D-INF-BACKEND: no in-cluster
inference — embed/rerank go to managed/external.)_

### D-NS — dedicated indexes allow only ONE populated namespace → RESOLVED (single namespace)

`control-plane/src/scheduler/v4_log.rs:357-364`: a Dedicated index permits at most **one
populated namespace** unless its `index_id` is in `provisioned_multi_namespace_allowed_index_ids`
(a control-plane allowlist). Since BYOC forces Dedicated, this collided with the
ephemeral-namespace lever and Nexus's `default` + `artifacts` layout.
**Decision (PoC): option (c) — single namespace per index, partitioned by metadata.**
Nexus already tags every record with a `kind` field (chunk vs. summary/topic/entity/…),
so `default` and `artifacts` collapse into one namespace filtered by `kind`; ephemeral
build runs isolate via an added build-id tag and are deleted by filter. Runtime change is
confined to `runtime/skills/pinecone/pinecone.py` (namespace constants → single namespace +
`kind`/build-id filters). _(Later: multi-namespace via the control-plane allowlist.)_

## 6. Suggested phasing

- **Phase 0 — Validate the data path (no IaC).** Point a local Nexus at a BYOC DB env:
  create a schema/document index via managed control plane, then exercise documents
  upsert/search against the returned in-cluster `host` with managed `/embed` + `/rerank`.
  Settles D-EP and D-FTS empirically.
- **Phase 1 — Nexus changes.** Schema/document index creation + documents API + explicit
  embed (§3.1–3.2), single deployment credential (§3.3), inference client lib (§3.4),
  Helm surface (§3.5). All gated on BYOC config so the managed deploy is undisturbed.
- **Phase 2 — Pulumi `Nexus` component.** Node pools, storage, image-registry wiring,
  Helm install, secret/cred injection, ingress (§4). One `pulumi up` brings up DB + Nexus.
- **Phase 3 — Wizard + versioning + hardening.** Combined release manifest, customer
  surface gating, the real Inference Proxy (BYOM, scoped creds, metering, boundary policy),
  per-user credentials, security review for task-pod isolation (design D5).

## 7. Open risks

- **Data path rework is the bulk of the work** — moving Nexus from integrated inference +
  `/vectors` to explicit-embed + documents API touches the core runtime. Phase 0 de-risks it.
- **`host` reachability (D-EP)** — if the returned host isn't in-cluster reachable from task
  pods, vector ops break; verify before building the IaC.
- **Image registry (D-REG)** — Nexus images must be published to and pullable from the BYOC
  registry via `regcred`; the `regcred` namespace coverage gap (§4.2) is the concrete task.
- **Two FoundationDB clusters** in one cluster (Nexus `b"nx"` + DB `pc-fdb`) — fine, but
  sizing/storage and node capacity must account for both.
- **Data boundary** — external inference is a PoC-only allowance; the proxy end-state must
  land before a regulated customer (Aon/Azure).
- **Observability** — Nexus must land on the cluster's DD agent; no log tailing from outside.

---

# Agent-executable breakdown

> The sections above are the decisions and rationale. The sections below are what an agent
> needs to implement them. **Guardrails:** the `nexus` repo's `CLAUDE.md` forbids running
> `docker build` / `cargo build` / `npm run build` / service restarts unless explicitly
> asked — make code changes and let the maintainer run heavy builds. Conventional commits,
> no emojis, no AI-assistant references. Every BYOC change must **gate on config** so the
> existing managed Nexus deploy is byte-for-byte unaffected when BYOC is off.

## 8. API contracts (target shapes)

> These are the target contracts for the BYOC schema/document model. Field-level details
> should be confirmed against the `2026-01.alpha` OpenAPI (or a Phase-0 probe) before coding;
> they are accurate as of the last verification but the alpha API can drift. **All calls send
> header `X-Pinecone-Api-Version: 2026-01.alpha`.**

**A. Create index (control plane, managed `POST {PINECONE_API_BASE}/indexes`)** — replaces
`create-for-model`. One per context, dense + FTS in one schema:
```jsonc
{
  "name": "nexus-ctx-<id>",
  "deployment": { "deployment_type": "byoc", "environment": "<BYOC env id, e.g. preprod-gcp-us-central1-XXXX.byoc>" },
  "schema": { "fields": {
    "embedding":   { "type": "dense_vector", "dimension": 1024, "metric": "cosine" },   // multilingual-e5-large = 1024
    "chunk_text":  { "type": "string", "full_text_search": { /* language/stemming/stop_words — confirm shape */ } }
  }},
  "read_capacity": { "mode": "Dedicated",
    "dedicated": { "node_type": "b1", "scaling": "Manual", "manual": { "shards": 1, "replicas": 1 } } },
  "deletion_protection": "disabled"
}
// response → .host  (use the `.svc.private.` form for in-cluster; see D-EP)
```
Note: `cloud`/`region` from the old body are **dropped**; placement comes from `deployment.environment`.

**B. Upsert (data plane, `POST https://{HOST}/namespaces/{ns}/documents/upsert`)** — vectors
supplied by us (no server-side embed). `_id` is the id key; the vector goes in the named
schema field; other fields are metadata:
```jsonc
{ "documents": [
  { "_id": "chunk-123", "embedding": [/* 1024 floats from /embed */],
    "chunk_text": "…raw text…", "kind": "chunk", "build_id": "b-2026..." }
]}
```

**C. Search (data plane, `POST https://{HOST}/namespaces/{ns}/documents/search`)** — **one
scoring method per request**; hybrid = two calls (dense, then FTS) merged client-side:
```jsonc
// dense leg
{ "score_by": [ { "type": "dense_vector", "field": "embedding", "values": [/* query vector */] } ],
  "top_k": 20, "filter": { "kind": { "$eq": "chunk" } }, "include_fields": ["chunk_text","kind"] }
// FTS leg — same shape with score_by type for the FTS field (confirm exact `type` token in alpha)
```

**D. Embed (managed `POST {INFERENCE_BASE}/embed`)** and **rerank (`/rerank`)** — the thin
inference lib. Models already in code: embed `multilingual-e5-large`, rerank `pinecone-rerank-v0`:
```jsonc
// /embed
{ "model": "multilingual-e5-large", "inputs": [ { "text": "…" } ],
  "parameters": { "input_type": "passage" /* or "query" */, "truncate": "END" } }
// /rerank  (note: pinecone-rerank-v0 accepts `truncate`; cohere does not — keep model-conditional)
{ "model": "pinecone-rerank-v0", "query": "…", "documents": [ { "text": "…" } ],
  "top_n": 5, "parameters": { "truncate": "END" } }
```

## 9. Execution plan (tasks)

Ordering matters within a phase; phases are sequential. Each task: **repo · files · DoD**.

**Phase 0 — Validate data path (no IaC; throwaway script, any language).**
- **0.1** Against a BYOC DB env: create index (§8A) → upsert (§8B) → dense+FTS search (§8C),
  using managed `/embed` for vectors. **DoD:** documents round-trip and the returned `.host`
  is reachable from a pod in the cluster (curl from an in-cluster debug pod). Settles D-EP/D-FTS.

**Phase 1 — Nexus code (repo: `nexus`). All gated behind a `PINECONE_BYOC` config flag.**
- **1.1 Config plumbing.** `common/src/config.rs` (+ Python `runtime/skills/agent_core/state.py`):
  add `PINECONE_API_BASE`, `PINECONE_BYOC` (bool), `PINECONE_BYOC_ENV` (the deployment env id),
  `INFERENCE_BASE`, `INFERENCE_API_KEY`. **DoD:** all read from env with managed defaults.
- **1.2 Rust control-plane client.** `common/src/pinecone.rs` + call sites
  (`api/src/rest/handlers/{auth,contexts}.rs`, `orchestrator/src/task_manager.rs`): use
  `with_base_url(PINECONE_API_BASE)`; when BYOC, emit the §8A body and drop `x-environment`.
  **DoD:** managed path unchanged (env off); BYOC path emits schema/Dedicated create.
- **1.3 Python index creation.** `runtime/optimize.py` (the `create-for-model` site): same
  §8A shape, base from `PINECONE_API_BASE`, env from config; remove hardcoded `gcp/us-central1`.
- **1.4 Inference lib.** New `runtime/skills/inference/` (embed/rerank/llm) per §8D, env-configured.
  Route existing rerank (`pinecone.py:877`) and the new embed through it. **DoD:** one module
  is the only place inference endpoints appear.
- **1.5 Data path → documents API.** `runtime/skills/pinecone/pinecone.py` (+ `curate`/`query`/
  `optimize` call sites): upsert/search per §8B/§8C; embed-then-upsert; **single namespace**,
  partition by `kind`, build runs tagged `build_id`. **DoD:** curate/query/optimize work end-to-end
  against a BYOC index in a local run; hybrid merges two search legs.
- **1.6 Single credential at login.** `api/src/rest/handlers/auth.rs` + `api/src/auth.rs`: when
  BYOC, source the key from `PINECONE_API_KEY` (config) instead of user input; keep populating
  the `pinecone_key` claim so downstream is untouched. **DoD:** login needs no user key in BYOC mode.
- **1.7 Helm surface.** `deploy/helm/nexus/values.yaml` + templates: expose the new config/secret;
  thread into api/orchestrator/knowql env and task-pod env. **DoD:** `helm template` renders the
  new env; values default to managed behavior.

**Phase 2 — Pulumi (repo: `pulumi-pinecone-byoc`).**
- **2.1 Node pools.** `pulumi_pinecone_byoc/gcp/gke.py`: add `nexus-role: services` / `jobs`
  pools (labels + taints matching the chart). **DoD:** pools present with correct labels/taints.
- **2.2 Mint Nexus key.** Reuse the `ApiKey`/`__SLI__` provider to mint the deployment key;
  add to `common/k8s_secrets.py` as a nexus secret. **DoD:** secret present in the nexus namespace.
- **2.3 `regcred` coverage.** `common/cred_refresher.py`: add `nexus`,`nexus-tasks` to
  `EXTRA_NAMESPACES`. **DoD:** `regcred` materializes in both namespaces.
- **2.4 `Nexus` component.** New `pulumi_pinecone_byoc/common/nexus.py` (parallel to `pinetools.py`):
  install `nexus-fdb` then `nexus` Helm releases with `image.registry`/`nexus-version`/
  `pullSecrets`, the minted key, inference + DB-env config; wire into `gcp/cluster.py` after the
  DB stack. **DoD:** `pulumi preview` shows the releases ordered after DB; storage class set (2.5).
- **2.5 Storage.** Set `persistence.storageClass` (and RWX/Filestore if used) from the cloud stack.
- **2.6 Ingress.** Route the Nexus `gateway` through the existing LB; keep DB data plane internal.
- **2.7 Wizard/versioning.** `setup/wizard.py` + `config/*`: Nexus-enable + inference config;
  add `nexus-version`. **DoD:** wizard generates a project that brings up both.

**Phase 3 — deferred** (real Inference Proxy, per-user creds, combined release manifest, D5
isolation hardening) — out of PoC scope; tracked in §0 _(later)_ rows.

## 10. New config / env surface (consolidated)

| Key | Where | Default (managed) | BYOC value |
|-----|-------|-------------------|------------|
| `PINECONE_BYOC` | nexus env / Helm | `false` | `true` |
| `PINECONE_API_BASE` | nexus env / Helm | `https://api.pinecone.io` | same (managed control plane) |
| `PINECONE_BYOC_ENV` | nexus env / Helm | unset | the `…​.byoc` env id |
| `PINECONE_API_KEY` | nexus secret | (user-supplied today) | the minted deployment key |
| `INFERENCE_BASE` | nexus env / Helm | `https://api.pinecone.io` | same (PoC) |
| `INFERENCE_API_KEY` | nexus secret | = `PINECONE_API_KEY` | = deployment key (PoC) |
| `nexus-version` | Pulumi config | n/a | image tag |
| node pools, storageClass, ingress | Pulumi config | n/a | per cluster |

## 11. Human / infra asks (NOT agent-doable — must be arranged in parallel)

- **Nexus image publishing.** Nexus CI must push `nexus_*` images to a repo under
  `us-docker.pkg.dev/pinecone-artifacts` tagged with `nexus-version` (today targets the managed
  GKE flow). Owner: Nexus/CI.
- **Registry IAM.** Grant the CPGW `/cr-token` service account **Artifact Registry Reader** on
  that repo (token is host-wide but IAM-gated — D-REG). Owner: DB/infra.
- **Dedicated-capacity flag.** Use an **Internal** org for the PoC (bypasses the gate) or have
  `ALLOW_DEDICATED_CAPACITY_USAGE` enabled for the org (D-CAP). Owner: control-plane/LD.
- **BYOC env provisioning.** A real `.byoc` environment to target (the PoC env id used in §8A).
  Owner: BYOC onboarding.
- **NetworkPolicy egress.** Ensure pod → internal-LB / gateway-proxy egress is permitted so the
  `.svc.private.` host resolves and connects (D-EP). Owner: whoever owns the cluster netpol.
