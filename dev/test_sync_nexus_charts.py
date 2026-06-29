"""Tests for the Nexus chart vendoring sync tool.

Run with: cd dev && uv run --with pytest python -m pytest test_sync_nexus_charts.py
"""

import subprocess
from pathlib import Path

import sync_nexus_charts as s


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_relative_files_lists_nested_paths(tmp_path: Path) -> None:
    _write(tmp_path, "Chart.yaml", "a")
    _write(tmp_path, "templates/svc.yaml", "b")
    assert s.relative_files(tmp_path) == {Path("Chart.yaml"), Path("templates/svc.yaml")}


def test_tree_diff_empty_when_identical(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "Chart.yaml", "x")
    _write(dst, "Chart.yaml", "x")
    assert s.tree_diff(src, dst) == []


def test_tree_diff_reports_missing_extra_and_changed(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "same.yaml", "1")
    _write(dst, "same.yaml", "1")
    _write(src, "only_src.yaml", "s")  # missing in dst
    _write(dst, "only_dst.yaml", "d")  # extra in dst
    _write(src, "changed.yaml", "new")
    _write(dst, "changed.yaml", "old")
    assert s.tree_diff(src, dst) == ["changed.yaml", "only_dst.yaml", "only_src.yaml"]


def test_mirror_tree_copies_new_nested_file(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "templates/deploy.yaml", "hello")
    changed = s.mirror_tree(src, dst)
    assert (dst / "templates/deploy.yaml").read_text() == "hello"
    assert changed == ["templates/deploy.yaml"]


def test_mirror_tree_prunes_extra_file(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "keep.yaml", "k")
    _write(dst, "keep.yaml", "k")
    _write(dst, "stale.yaml", "gone")
    changed = s.mirror_tree(src, dst)
    assert not (dst / "stale.yaml").exists()
    assert changed == ["stale.yaml"]


def test_mirror_tree_overwrites_changed_then_idempotent(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "Chart.yaml", "new")
    _write(dst, "Chart.yaml", "old")
    assert s.mirror_tree(src, dst) == ["Chart.yaml"]
    assert (dst / "Chart.yaml").read_text() == "new"
    assert s.mirror_tree(src, dst) == []  # second run is a no-op


def test_write_provenance_records_sha(tmp_path: Path) -> None:
    s.write_provenance(tmp_path, "abc123")
    stamp = (tmp_path / "CHARTS_SOURCE").read_text()
    assert "abc123" in stamp


def test_nexus_sha_reads_git_head(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert s.nexus_sha(tmp_path) == head
