"""DefaultWorkspace provider: Ready-wait loop, failure modes, first-run semantics."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common import providers  # noqa: E402
from pulumi_pinecone_byoc.common.api import (  # noqa: E402
    PineconeApiInternalError,
    WorkspaceResponse,
)

_PROPS = {
    "name": "default",
    "environment": "gcp-us-central1-ab12",
    "api_url": "https://api.pinecone.io",
    "pinecone_api_key": "key-1",
    "host": None,
    "url": None,
}

_HOST = "default-jjjstks.wksp.gcp-us-central1-ab12.pinecone.io"


def _ws(state: str) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(
        {"name": "default", "host": _HOST, "status": {"ready": state == "Ready", "state": state}}
    )


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _create_with(create_result, get_results):
    clock = _FakeClock()
    with (
        patch.object(providers, "create_workspace", return_value=create_result),
        patch.object(providers, "get_workspace", side_effect=get_results),
        patch.object(providers.time, "monotonic", clock.monotonic),
        patch.object(providers.time, "sleep", clock.sleep),
    ):
        return providers.DefaultWorkspaceProvider().create(dict(_PROPS))


def test_create_waits_until_ready():
    result = _create_with(_ws("Initializing"), [_ws("Initializing"), _ws("Ready")])
    assert result.id == "default"
    assert result.outs["host"] == _HOST
    assert result.outs["url"] == f"https://{_HOST}/contexts"


def test_create_immediately_ready_skips_polling():
    result = _create_with(_ws("Ready"), [])
    assert result.outs["url"] == f"https://{_HOST}/contexts"


def test_initialization_failed_raises():
    try:
        _create_with(_ws("Initializing"), [_ws("InitializationFailed")])
    except Exception as e:
        assert "failed to initialize" in str(e)
    else:
        raise AssertionError("expected failure")


def test_timeout_raises_with_last_state():
    # get_workspace forever returns Initializing; the fake clock advances 15s per
    # sleep, so the 900s deadline trips after 60 polls.
    try:
        _create_with(_ws("Initializing"), [_ws("Initializing")] * 100)
    except Exception as e:
        assert "timed out" in str(e)
        assert "Initializing" in str(e)
    else:
        raise AssertionError("expected timeout")


def test_create_tolerates_transient_get_workspace_error():
    result = _create_with(
        _ws("Initializing"),
        [PineconeApiInternalError("boom"), _ws("Initializing"), _ws("Ready")],
    )
    assert result.id == "default"
    assert result.outs["host"] == _HOST
    assert result.outs["url"] == f"https://{_HOST}/contexts"


def test_create_does_not_echo_api_key_in_outs():
    result = _create_with(_ws("Ready"), [])
    assert "pinecone_api_key" not in result.outs


def test_diff_never_reports_changes():
    diff = providers.DefaultWorkspaceProvider().diff("default", dict(_PROPS), dict(_PROPS))
    assert diff.changes is False
    changed = {**_PROPS, "environment": "other-env", "pinecone_api_key": "rotated"}
    diff = providers.DefaultWorkspaceProvider().diff("default", dict(_PROPS), changed)
    assert diff.changes is False


def test_delete_is_noop():
    providers.DefaultWorkspaceProvider().delete("default", dict(_PROPS))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
