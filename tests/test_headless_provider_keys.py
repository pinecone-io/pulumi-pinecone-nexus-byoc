"""Guards the headless provider-key contract (issue #34).

Headless generation must supply the inference-proxy provider keys the effective
catalog references, or fail loudly -- never silently ship blank credentials.
Covers the PINECONE_NEXUS_PROVIDER_KEYS JSON contract, the missing-key hard
error, and the explicit opt-out. The helper is shared, so exercising one wizard
covers all three clouds.

Run standalone (`python tests/test_headless_provider_keys.py`) or under pytest.
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

from wizard import BaseSetupWizard  # noqa: E402

# Env vars the tests set; cleared around every case so they don't leak.
_MANAGED_ENV = (
    "PINECONE_NEXUS_ENABLED",
    "PINECONE_BYOC_PROJECT_ID",
    "PINECONE_NEXUS_PROVIDER_KEYS",
    "PINECONE_NEXUS_ALLOW_MISSING_PROVIDER_KEYS",
    "PINECONE_NEXUS_LLM_MODELS",
    "PINECONE_NEXUS_RERANK_MODELS",
    "PINECONE_NEXUS_LLM_LITE",
    "PINECONE_NEXUS_LLM_STANDARD",
    "PINECONE_NEXUS_LLM_PRO",
    "PINECONE_NEXUS_RERANK_MODEL",
)

_UUID = "123e4567-e89b-12d3-a456-426614174000"


@contextlib.contextmanager
def _env(**overrides: str):
    """Set env vars for the block, clearing every managed var first."""
    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    try:
        for k in _MANAGED_ENV:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _wizard() -> BaseSetupWizard:
    return BaseSetupWizard(headless=True)


# ----- _headless_provider_keys (unit) --------------------------------------


def test_json_map():
    with _env(PINECONE_NEXUS_PROVIDER_KEYS='{"gemini-api-key": "g-secret"}'):
        keys = _wizard()._headless_provider_keys({"gemini-api-key"})
    assert keys == {"gemini-api-key": "g-secret"}


def test_multiple_refs_all_populated():
    with _env(
        PINECONE_NEXUS_PROVIDER_KEYS='{"openai-api-key": "o", "cohere-api-key": "c"}',
    ):
        keys = _wizard()._headless_provider_keys({"openai-api-key", "cohere-api-key"})
    assert keys == {"openai-api-key": "o", "cohere-api-key": "c"}


def test_stray_json_entries_dropped():
    with _env(
        PINECONE_NEXUS_PROVIDER_KEYS='{"gemini-api-key": "g", "unused-api-key": "x"}',
    ):
        keys = _wizard()._headless_provider_keys({"gemini-api-key"})
    assert keys == {"gemini-api-key": "g"}


def test_missing_key_is_hard_error():
    with _env():
        keys = _wizard()._headless_provider_keys({"gemini-api-key"})
    assert keys is None


def test_blank_value_treated_as_missing():
    with _env(PINECONE_NEXUS_PROVIDER_KEYS='{"gemini-api-key": "   "}'):
        keys = _wizard()._headless_provider_keys({"gemini-api-key"})
    assert keys is None


def test_allow_missing_opt_out():
    with _env(PINECONE_NEXUS_ALLOW_MISSING_PROVIDER_KEYS="true"):
        keys = _wizard()._headless_provider_keys({"gemini-api-key"})
    assert keys == {}


def test_partial_missing_with_opt_out():
    with _env(
        PINECONE_NEXUS_PROVIDER_KEYS='{"openai-api-key": "o"}',
        PINECONE_NEXUS_ALLOW_MISSING_PROVIDER_KEYS="true",
    ):
        keys = _wizard()._headless_provider_keys({"openai-api-key", "cohere-api-key"})
    assert keys == {"openai-api-key": "o"}


def test_invalid_json_raises():
    with _env(PINECONE_NEXUS_PROVIDER_KEYS="{not json"):
        try:
            _wizard()._headless_provider_keys({"gemini-api-key"})
        except ValueError:
            return
    raise AssertionError("expected ValueError for invalid PINECONE_NEXUS_PROVIDER_KEYS JSON")


def test_non_object_json_raises():
    with _env(PINECONE_NEXUS_PROVIDER_KEYS='["gemini-api-key"]'):
        try:
            _wizard()._headless_provider_keys({"gemini-api-key"})
        except ValueError:
            return
    raise AssertionError("expected ValueError when PINECONE_NEXUS_PROVIDER_KEYS is not an object")


def test_no_refs_returns_empty():
    with _env():
        keys = _wizard()._headless_provider_keys(set())
    assert keys == {}


# ----- _headless_nexus_config (integration) --------------------------------


def test_headless_config_default_catalog_populates_gemini():
    """Acceptance: default catalog + a Gemini key => provider_keys set."""
    with _env(
        PINECONE_NEXUS_ENABLED="true",
        PINECONE_BYOC_PROJECT_ID=_UUID,
        PINECONE_NEXUS_PROVIDER_KEYS='{"gemini-api-key": "g-secret"}',
    ):
        cfg = _wizard()._headless_nexus_config()
    assert cfg is not None
    assert cfg["enabled"] is True
    assert cfg["provider_keys"] == {"gemini-api-key": "g-secret"}


def test_headless_config_missing_gemini_aborts():
    """Acceptance: a referenced ref missing from the env aborts the run."""
    with _env(PINECONE_NEXUS_ENABLED="true", PINECONE_BYOC_PROJECT_ID=_UUID):
        cfg = _wizard()._headless_nexus_config()
    assert cfg is None


def test_headless_config_custom_catalog_multiple_providers():
    """Acceptance: custom catalog referencing multiple providers => every ref set."""
    llm = (
        '{"gpt-4o": {"api_style": "openai", "model": "gpt-4o", "label": "GPT-4o",'
        ' "provider": "openai", "api_key_ref": "openai-api-key"},'
        ' "cmd-r": {"api_style": "litellm", "model": "cohere/command-r", "label": "Command R",'
        ' "provider": "cohere", "api_key_ref": "cohere-api-key"},'
        ' "gpt-4o-mini": {"api_style": "openai", "model": "gpt-4o-mini", "label": "GPT-4o mini",'
        ' "provider": "openai", "api_key_ref": "openai-api-key"}}'
    )
    rerank = '{"bge-reranker-v2-m3": {"api_style": "pinecone", "model": "bge-reranker-v2-m3"}}'
    with _env(
        PINECONE_NEXUS_ENABLED="true",
        PINECONE_BYOC_PROJECT_ID=_UUID,
        PINECONE_NEXUS_LLM_MODELS=llm,
        PINECONE_NEXUS_RERANK_MODELS=rerank,
        PINECONE_NEXUS_LLM_LITE="gpt-4o-mini",
        PINECONE_NEXUS_LLM_STANDARD="gpt-4o",
        PINECONE_NEXUS_LLM_PRO="cmd-r",
        PINECONE_NEXUS_RERANK_MODEL="bge-reranker-v2-m3",
        PINECONE_NEXUS_PROVIDER_KEYS='{"openai-api-key": "o", "cohere-api-key": "c"}',
    ):
        cfg = _wizard()._headless_nexus_config()
    assert cfg is not None
    assert cfg["provider_keys"] == {"openai-api-key": "o", "cohere-api-key": "c"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
