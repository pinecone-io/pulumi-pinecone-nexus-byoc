#!/usr/bin/env python3
"""Vendor the Nexus Helm charts into the BYOC package.

Single source of truth is the **nexus** repo (``nexus/deploy/helm/{nexus,nexus-fdb}``).
This mirrors those chart trees into ``pulumi_pinecone_byoc/charts/`` so they ship in the
published wheel and resolve in a standalone ``git clone`` -- without the sibling-repo
symlink. A ``CHARTS_SOURCE`` provenance stamp records the nexus commit the copy came from.

Usage (run from the combined workspace, where ``nexus/`` is a sibling of this repo)::

    python dev/sync_nexus_charts.py            # mirror + restamp
    python dev/sync_nexus_charts.py --check     # exit non-zero if the copy is stale (CI)
    python dev/sync_nexus_charts.py --nexus-root /path/to/nexus   # override source

The release build packages whatever is committed -- run this and commit the result
*before* tagging a release (the release workflow has no ``nexus/`` checkout).
"""

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

# Chart dirs to vendor, relative to ``<nexus>/deploy/helm`` and ``<pkg>/charts``.
CHARTS: tuple[str, ...] = ("nexus", "nexus-fdb")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_NEXUS_ROOT = _REPO_ROOT.parent / "nexus"
_HELM_SUBDIR = Path("deploy") / "helm"
_DEST_CHARTS = _REPO_ROOT / "pulumi_pinecone_byoc" / "charts"
_STAMP_NAME = "CHARTS_SOURCE"


def relative_files(root: Path) -> set[Path]:
    """Every file under ``root`` as a path relative to ``root``."""
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def tree_diff(src: Path, dst: Path) -> list[str]:
    """Sorted rel-path strings where ``dst`` differs from ``src``.

    A path is reported when it is missing from ``dst``, extra in ``dst``, or present
    in both with differing bytes.
    """
    src_files = relative_files(src) if src.exists() else set()
    dst_files = relative_files(dst) if dst.exists() else set()
    differing = src_files ^ dst_files
    for rel in src_files & dst_files:
        if not filecmp.cmp(src / rel, dst / rel, shallow=False):
            differing.add(rel)
    return sorted(str(p) for p in differing)


def mirror_tree(src: Path, dst: Path) -> list[str]:
    """Make ``dst`` byte-identical to ``src`` (copy new/changed, prune extra).

    Returns the sorted rel-path strings that were added, updated, or removed.
    """
    changed = tree_diff(src, dst)
    for rel in changed:
        target = dst / rel
        source = src / rel
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            target.unlink()
    return changed


def nexus_sha(nexus_root: Path) -> str:
    """Full git HEAD SHA of the nexus checkout at ``nexus_root``."""
    return subprocess.run(
        ["git", "-C", str(nexus_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_provenance(dst_charts: Path, sha: str) -> None:
    """Write ``<dst_charts>/CHARTS_SOURCE`` recording the source nexus commit."""
    dst_charts.mkdir(parents=True, exist_ok=True)
    (dst_charts / _STAMP_NAME).write_text(
        "# Vendored from pinecone-io/nexus deploy/helm by dev/sync_nexus_charts.py.\n"
        "# Do not edit by hand -- re-run the sync to refresh.\n"
        f"nexus_sha = {sha}\n"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the vendored copy differs from the nexus source (no writes)",
    )
    parser.add_argument(
        "--nexus-root",
        type=Path,
        default=_DEFAULT_NEXUS_ROOT,
        help="path to the nexus repo checkout (default: sibling of this repo)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    helm_root = args.nexus_root / _HELM_SUBDIR
    if not helm_root.is_dir():
        print(f"error: nexus charts not found at {helm_root}", file=sys.stderr)
        return 2

    if args.check:
        stale: list[str] = []
        for chart in CHARTS:
            stale += [f"{chart}/{p}" for p in tree_diff(helm_root / chart, _DEST_CHARTS / chart)]
        if stale:
            print("vendored charts are stale; run dev/sync_nexus_charts.py:", file=sys.stderr)
            for p in stale:
                print(f"  {p}", file=sys.stderr)
            return 1
        print("vendored charts are in sync with nexus source")
        return 0

    total = 0
    for chart in CHARTS:
        changed = mirror_tree(helm_root / chart, _DEST_CHARTS / chart)
        total += len(changed)
        for p in changed:
            print(f"  {chart}/{p}")
    write_provenance(_DEST_CHARTS, nexus_sha(args.nexus_root))
    print(f"synced {len(CHARTS)} charts ({total} file change(s)) from {args.nexus_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
