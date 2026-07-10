"""Workspace create/fetch API calls: header shape, 409-resume, 403 gate message."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.api import (  # noqa: E402
    WORKSPACES_NOT_ENABLED_MSG,
    PineconeApiError,
    create_workspace,
    get_workspace,
)

_WS_BODY = {
    "name": "default",
    "spec": {"byoc": {"environment": "gcp-us-central1-ab12"}},
    "host": "default-jjjstks.wksp.gcp-us-central1-ab12.pinecone.io",
    "created_at": "2026-07-10T00:00:00Z",
    "updated_at": "2026-07-10T00:00:00Z",
    "status": {"ready": False, "state": "Initializing"},
}


def test_create_workspace_posts_unstable_and_parses():
    with patch("pulumi_pinecone_byoc.common.api.request", return_value=_WS_BODY) as req:
        ws = create_workspace("key-1", "https://api.pinecone.io", "default", "gcp-us-central1-ab12")
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "https://api.pinecone.io/workspaces"
    assert kwargs["headers"]["Api-Key"] == "key-1"
    assert kwargs["headers"]["X-Pinecone-Api-Version"] == "unstable"  # case matches api.py's management_plane_headers
    assert kwargs["body"] == {
        "name": "default",
        "spec": {"byoc": {"environment": "gcp-us-central1-ab12"}},
    }
    assert ws.host == _WS_BODY["host"]
    assert ws.status.state == "Initializing"
    assert ws.status.ready is False


def test_create_workspace_409_falls_through_to_get():
    calls = []

    def fake_request(method, url, headers=None, body=None):
        calls.append(method)
        if method == "POST":
            raise PineconeApiError(409, "409: workspace already exists")
        return _WS_BODY

    with patch("pulumi_pinecone_byoc.common.api.request", side_effect=fake_request):
        ws = create_workspace("key-1", "https://api.pinecone.io", "default", "gcp-us-central1-ab12")
    assert calls == ["POST", "GET"]
    assert ws.name == "default"


def test_create_workspace_403_raises_actionable_message():
    with patch(
        "pulumi_pinecone_byoc.common.api.request",
        side_effect=PineconeApiError(403, "403: Forbidden"),
    ):
        try:
            create_workspace("key-1", "https://api.pinecone.io", "default", "env")
        except PineconeApiError as e:
            assert e.code == 403
            assert e.msg == WORKSPACES_NOT_ENABLED_MSG
        else:
            raise AssertionError("expected PineconeApiError")


def test_get_workspace_url_and_parse():
    with patch("pulumi_pinecone_byoc.common.api.request", return_value=_WS_BODY) as req:
        ws = get_workspace("key-1", "https://api.pinecone.io", "default")
    args, _kwargs = req.call_args
    assert args[0] == "GET"
    assert args[1] == "https://api.pinecone.io/workspaces/default"
    assert ws.host == _WS_BODY["host"]


def test_get_workspace_invalid_response_raises():
    with patch("pulumi_pinecone_byoc.common.api.request", return_value={"nope": True}):
        try:
            get_workspace("key-1", "https://api.pinecone.io", "default")
        except PineconeApiError as e:
            assert e.code == 500
        else:
            raise AssertionError("expected PineconeApiError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
