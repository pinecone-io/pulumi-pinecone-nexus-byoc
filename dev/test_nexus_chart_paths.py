# dev/test_nexus_chart_paths.py
# Run: cd pulumi-pinecone-nexus-byoc && uv run --with pytest python -m pytest dev/test_nexus_chart_paths.py
import importlib


def test_installed_charts_version_matches_metadata():
    from importlib.metadata import version

    from pulumi_pinecone_byoc.common import nexus

    assert nexus.installed_charts_version() == version("pinecone-nexus-charts")


def test_env_override_wins(tmp_path, monkeypatch):
    (tmp_path / "nexus").mkdir()
    (tmp_path / "nexus-fdb").mkdir()
    monkeypatch.setenv("PINECONE_NEXUS_CHARTS_PATH", str(tmp_path))
    from pulumi_pinecone_byoc.common import nexus

    importlib.reload(nexus)
    assert str(tmp_path / "nexus") == nexus._NEXUS_CHART
    assert str(tmp_path / "nexus-fdb") == nexus._NEXUS_FDB_CHART
