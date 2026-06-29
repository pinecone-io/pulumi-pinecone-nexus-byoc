# Task B4 Report: Default Nexus image tag to installed charts version

## Files Changed

1. `pulumi_pinecone_byoc/common/nexus.py` — added `from importlib.metadata import version as _pkg_version` and new module-level function `installed_charts_version() -> str`.
2. `pulumi_pinecone_byoc/gcp/cluster.py` — added `installed_charts_version` to the import from `..common.nexus`; changed `nexus_version=nx.version or args.pinecone_version` to `nexus_version=nx.version or installed_charts_version()`.
3. `pulumi_pinecone_byoc/azure/cluster.py` — identical import + fallback change as GCP.
4. `setup/wizard.py` — added `import importlib.metadata`; changed `NEXUS_VERSION = PINECONE_VERSION` to `NEXUS_VERSION = importlib.metadata.version("pinecone-nexus-charts")` with updated comment.
5. `dev/test_nexus_chart_paths.py` — added `test_installed_charts_version_matches_metadata`; ruff auto-fixed import sort, removed unused `pathlib.Path`, and flipped "Yoda condition" asserts in `test_env_override_wins`.

## Wizard Change and Rationale

**Change:** Line 65 was `NEXUS_VERSION = PINECONE_VERSION`. Now it is:
```python
NEXUS_VERSION = importlib.metadata.version("pinecone-nexus-charts")
```
with an updated comment explaining that Nexus is versioned independently of `PINECONE_VERSION`.

**Why:** The wizard computes `NEXUS_VERSION` at module import time and uses it as:
- The default for the `PINECONE_NEXUS_VERSION` env override in headless paths (lines 2053, 3164).
- The interactive prompt default shown to the operator (line 2285).
- The fallback written into the generated `Pulumi.<stack>.yaml` (lines 2499, 3473): `nexus-version: <version>`.

Because the wizard explicitly writes `nexus-version` to the stack config, simply not writing it (the other option) would have been a more invasive change (touching the config-generation blocks). Deriving `NEXUS_VERSION` from the installed package keeps the `PINECONE_NEXUS_VERSION` env override working and leaves the operator able to see exactly which tag will be deployed. The wizard is always installed in the same environment as `pinecone-nexus-charts`, so `importlib.metadata.version` is always resolvable.

## Test Command and Output

```
cd pulumi-pinecone-nexus-byoc && .venv/bin/python -m pytest dev/test_nexus_chart_paths.py -v
```

```
collected 2 items

dev/test_nexus_chart_paths.py::test_installed_charts_version_matches_metadata PASSED [ 50%]
dev/test_nexus_chart_paths.py::test_env_override_wins PASSED             [100%]

2 passed in 0.36s
```

## Ruff Result

```
All checks passed!
5 files already formatted
```

## Self-Review

- `installed_charts_version()` is a plain module-level function (not a `@staticmethod`) because both cluster files already import module functions from `..common.nexus`; this is consistent.
- The brief mentioned exposing it as a `@staticmethod` on `Nexus` as an alternative. The module-function approach was chosen for consistency with `derive_api_key_refs` and `_resolve_charts_root`.
- `NexusConfig.version: str | None = None` docstring says "falls back to pinecone_version" — that comment is now stale. Left as-is since touching it risks scope creep; the cluster files are the canonical truth.

## Concerns

None material. The decoupling is intentional and approved. The wizard writes the resolved charts version explicitly into the stack config (which is the right UX — the operator sees the exact tag at setup time). The `PINECONE_NEXUS_VERSION` env override still works in CI/headless flows.
