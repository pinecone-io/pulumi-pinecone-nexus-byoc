"""Guards headless Nexus provider-key wiring.

Regression cover for the gap where the GCP/Azure headless paths built the
`nexus` config WITHOUT `gemini_api_key` (nor `provider_keys`), so
`_generate_project` never set the `nexus-gemini-api-key` /
`nexus-provider-keys.*` secrets and Nexus deployed with no generation model.

Run standalone (`python tests/test_headless_nexus_secrets.py`) or under pytest.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

from wizard import (  # noqa: E402
    AzureSetupWizard,
    BaseSetupWizard,
    GCPSetupWizard,
)

_UUID = "123e4567-e89b-12d3-a456-426614174000"


# --- helper units ---------------------------------------------------------


def test_headless_gemini_api_key_reads_pinecone_var():
    with mock.patch.dict(os.environ, {"PINECONE_GEMINI_API_KEY": " gm-key "}, clear=True):
        assert BaseSetupWizard(headless=True)._headless_gemini_api_key() == "gm-key"


def test_headless_gemini_api_key_falls_back_to_gemini_var():
    with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gm-fallback"}, clear=True):
        assert BaseSetupWizard(headless=True)._headless_gemini_api_key() == "gm-fallback"


def test_headless_gemini_api_key_prefers_pinecone_var():
    env = {"PINECONE_GEMINI_API_KEY": "primary", "GEMINI_API_KEY": "secondary"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert BaseSetupWizard(headless=True)._headless_gemini_api_key() == "primary"


def test_headless_gemini_api_key_missing_returns_empty():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert BaseSetupWizard(headless=True)._headless_gemini_api_key() == ""


def test_headless_provider_keys_empty_by_default():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert BaseSetupWizard(headless=True)._headless_provider_keys() == {}


def test_headless_provider_keys_parses_json_and_drops_blanks():
    env = {"PINECONE_NEXUS_PROVIDER_KEYS": '{"openai-api-key": "sk-1", "unused": "  "}'}
    with mock.patch.dict(os.environ, env, clear=True):
        assert BaseSetupWizard(headless=True)._headless_provider_keys() == {
            "openai-api-key": "sk-1"
        }


def test_headless_provider_keys_rejects_bad_json():
    with mock.patch.dict(os.environ, {"PINECONE_NEXUS_PROVIDER_KEYS": "{not json"}, clear=True):
        try:
            BaseSetupWizard(headless=True)._headless_provider_keys()
        except ValueError:
            return
        raise AssertionError("expected ValueError on invalid JSON")


def test_headless_provider_keys_rejects_non_string_values():
    with mock.patch.dict(os.environ, {"PINECONE_NEXUS_PROVIDER_KEYS": '{"k": 1}'}, clear=True):
        try:
            BaseSetupWizard(headless=True)._headless_provider_keys()
        except ValueError:
            return
        raise AssertionError("expected ValueError on non-string value")


# --- end-to-end wiring (would have caught the original bug) ----------------


def _capture_nexus(wiz):
    """Replace _generate_project (which shells out to pulumi) with a capture so
    _run_headless can be exercised offline. Returns a dict the call fills in."""
    captured = {}

    def fake_generate_project(*args, **kwargs):
        # nexus is the final positional arg in both the GCP and Azure calls.
        captured["nexus"] = kwargs.get("nexus", args[-1] if args else None)
        return True

    wiz._generate_project = fake_generate_project
    return captured


def test_gcp_headless_wires_gemini_and_provider_keys():
    env = {
        "PINECONE_API_KEY": "pc-key",
        "GCP_PROJECT": "my-gcp-project",
        "PINECONE_NEXUS_ENABLED": "true",
        "PINECONE_BYOC_PROJECT_ID": _UUID,
        "PINECONE_GEMINI_API_KEY": "gm-key",
        "PINECONE_NEXUS_PROVIDER_KEYS": '{"openai-api-key": "sk-1"}',
    }
    with mock.patch.dict(os.environ, env, clear=True):
        wiz = GCPSetupWizard(headless=True)
        captured = _capture_nexus(wiz)
        assert wiz._run_headless(".") is True

    nexus = captured["nexus"]
    assert nexus["enabled"] is True
    assert nexus["gemini_api_key"] == "gm-key", "headless must thread the Gemini key"
    assert nexus["provider_keys"] == {"openai-api-key": "sk-1"}


def test_azure_headless_wires_gemini_key():
    env = {
        "PINECONE_API_KEY": "pc-key",
        "AZURE_SUBSCRIPTION_ID": "sub-123",
        "PINECONE_NEXUS_ENABLED": "true",
        "PINECONE_BYOC_PROJECT_ID": _UUID,
        "GEMINI_API_KEY": "gm-key",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        wiz = AzureSetupWizard(headless=True)
        captured = _capture_nexus(wiz)
        assert wiz._run_headless(".") is True

    nexus = captured["nexus"]
    assert nexus["enabled"] is True
    assert nexus["gemini_api_key"] == "gm-key", "headless must thread the Gemini key"


def test_gcp_headless_nexus_disabled_has_no_gemini_key():
    """DB-only headless installs stay unchanged -- no Nexus keys threaded."""
    env = {
        "PINECONE_API_KEY": "pc-key",
        "GCP_PROJECT": "my-gcp-project",
        "PINECONE_NEXUS_ENABLED": "false",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        wiz = GCPSetupWizard(headless=True)
        captured = _capture_nexus(wiz)
        assert wiz._run_headless(".") is True

    assert captured["nexus"] == {"enabled": False}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
