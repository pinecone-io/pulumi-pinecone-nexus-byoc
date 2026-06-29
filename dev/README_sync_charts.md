# Vendoring the Nexus Helm charts

The Pulumi component installs two Helm charts — `nexus` and `nexus-fdb` — via
`helm.v3.Release` with **local chart paths** (`pulumi_pinecone_byoc/common/nexus.py`).
Those charts are **vendored copies** committed under
`pulumi_pinecone_byoc/charts/{nexus,nexus-fdb}/` so they ship inside the published wheel and
resolve in a standalone `git clone` — without the old `nexus -> ../nexus` sibling symlink
(which only resolved inside the combined workspace).

The single source of truth is the **`nexus` repo** (`nexus/deploy/helm/{nexus,nexus-fdb}`).
`dev/sync_nexus_charts.py` mirrors it into the package and stamps the source commit into
`pulumi_pinecone_byoc/charts/CHARTS_SOURCE`.

## Usage

Run from the combined workspace, where `nexus/` is a sibling of this repo:

```bash
python dev/sync_nexus_charts.py            # mirror charts + refresh CHARTS_SOURCE
python dev/sync_nexus_charts.py --check     # exit non-zero if the copy is stale (no writes)
python dev/sync_nexus_charts.py --nexus-root /path/to/nexus   # override the source location
```

Tests: `cd dev && uv run --with pytest python -m pytest test_sync_nexus_charts.py`

## The drift gate (must run where both repos exist)

The release workflow (`.github/workflows/release.yaml`) checks out **only this repo** — no
`nexus/` sibling — so the sync and `--check` **cannot** run at release time. Therefore:

1. **Before tagging a release**, run `dev/sync_nexus_charts.py` in the combined workspace and
   **commit the updated `charts/` + `CHARTS_SOURCE`**. The release build packages whatever is
   committed.
2. Gate the tag push on `dev/sync_nexus_charts.py --check` passing.
3. *(Optional, follow-up)* a CI job that `git clone --depth 1` of `pinecone-io/nexus` at the
   `CHARTS_SOURCE` SHA and runs `--check` — needs a nexus-read token in CI (infra decision).

## Chart ↔ image version coupling (do not let it drift)

The Nexus **image** is pinned via `nexus-version`; the vendored **chart** is a copy of
`nexus/deploy/helm` at the commit recorded in `CHARTS_SOURCE`. They must correspond. **Bump
the vendored chart and the `nexus-version` pin together, from the same `nexus` commit** — the
`CHARTS_SOURCE` stamp makes the chart side checkable.
