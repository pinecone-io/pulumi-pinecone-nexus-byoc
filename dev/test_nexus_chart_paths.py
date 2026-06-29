# dev/test_nexus_chart_paths.py
# Run: cd pulumi-pinecone-nexus-byoc && uv run --with pytest python -m pytest dev/test_nexus_chart_paths.py
import importlib
from pathlib import Path


def test_env_override_wins(tmp_path, monkeypatch):
    (tmp_path / "nexus").mkdir()
    (tmp_path / "nexus-fdb").mkdir()
    monkeypatch.setenv("PINECONE_NEXUS_CHARTS_PATH", str(tmp_path))
    from pulumi_pinecone_byoc.common import nexus
    importlib.reload(nexus)
    assert nexus._NEXUS_CHART == str(tmp_path / "nexus")
    assert nexus._NEXUS_FDB_CHART == str(tmp_path / "nexus-fdb")
