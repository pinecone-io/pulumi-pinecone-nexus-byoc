"""Guards the curate-window contract: supported_curate_models ⊆ supported_llm_models.

Run standalone (`python tests/test_inference_models_toml.py`) or under pytest.
"""

import os
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

from wizard import (  # noqa: E402
    _DEFAULT_EMBEDDING_MODELS,
    _DEFAULT_LLM_MODELS,
    _DEFAULT_RERANK_MODELS,
    NEXUS_INFERENCE_MODELS_TEMPLATE,
    _validate_surface_catalog,
    build_inference_models_toml,
)

_LLM = {
    "gemini-3.1-flash-lite": {
        "api_style": "litellm",
        "model": "gemini/gemini-3.1-flash-lite",
        "label": "Flash Lite",
        "provider": "gemini",
    },
    "gemini-3.5-flash": {
        "api_style": "litellm",
        "model": "gemini/gemini-3.5-flash",
        "label": "Flash",
        "provider": "gemini",
    },
    "gemini-3.1-pro-preview": {
        "api_style": "litellm",
        "model": "gemini/gemini-3.1-pro-preview",
        "label": "Pro",
        "provider": "gemini",
    },
}
_RERANK = {"bge-reranker-v2-m3": {"api_style": "pinecone", "model": "bge-reranker-v2-m3"}}
_TIERS = {
    "lite": "gemini-3.1-flash-lite",
    "standard": "gemini-3.5-flash",
    "pro": "gemini-3.1-pro-preview",
    "rerank": "bge-reranker-v2-m3",
}


def test_generator_curate_is_llm_minus_pro():
    profile = tomllib.loads(build_inference_models_toml(_LLM, _RERANK, _TIERS))["default"]
    llm = set(profile["supported_llm_models"])
    curate = set(profile["supported_curate_models"])
    assert curate <= llm, "curate window must be a subset of the LLM window"
    assert _TIERS["pro"] not in curate, "pro tier must be excluded from curate"
    assert curate == llm - {_TIERS["pro"]}


def test_generator_keeps_extra_non_pro_models_in_curate():
    llm = {
        **_LLM,
        "gemini-3.5-flash-preview": {
            "api_style": "litellm",
            "model": "x",
            "label": "Flash Preview",
            "provider": "gemini",
        },
    }
    profile = tomllib.loads(build_inference_models_toml(llm, _RERANK, _TIERS))["default"]
    assert "gemini-3.5-flash-preview" in set(profile["supported_curate_models"])


def test_static_template_curate_subset():
    profile = tomllib.loads(NEXUS_INFERENCE_MODELS_TEMPLATE)["default"]
    llm = set(profile["supported_llm_models"])
    curate = set(profile["supported_curate_models"])
    assert curate and curate <= llm
    assert "gemini-3.1-pro-preview" not in curate


def test_embedding_defaults_when_omitted():
    parsed = tomllib.loads(build_inference_models_toml(_LLM, _RERANK, _TIERS))
    assert "multilingual-e5-large" in parsed["embedding_models"]
    assert parsed["default"]["supported_embedding_models"] == ["multilingual-e5-large"]
    assert (
        parsed["default"]["embedding"]["tiers"]["default"]["model_ref"] == "multilingual-e5-large"
    )
    # dimension is a required model-level field (nexus#1234).
    assert parsed["embedding_models"]["multilingual-e5-large"]["dimension"] == 1024


def test_custom_embedding_model_is_emitted_and_routed():
    embedding = {
        "my-embed": {
            "api_style": "litellm",
            "model": "openai/text-embedding-3-large",
            "dimension": 3072,
        },
        "multilingual-e5-large": {
            "api_style": "pinecone",
            "model": "multilingual-e5-large",
            "dimension": 1024,
        },
    }
    tiers = {**_TIERS, "embedding": "my-embed"}
    parsed = tomllib.loads(
        build_inference_models_toml(_LLM, _RERANK, tiers, embedding_models=embedding)
    )
    assert set(parsed["embedding_models"]) == {"my-embed", "multilingual-e5-large"}
    assert parsed["embedding_models"]["my-embed"]["dimension"] == 3072
    assert set(parsed["default"]["supported_embedding_models"]) == {
        "my-embed",
        "multilingual-e5-large",
    }
    assert parsed["default"]["embedding"]["tiers"]["default"]["model_ref"] == "my-embed"


def test_embedding_model_requires_dimension():
    embedding = {"my-embed": {"api_style": "litellm", "model": "openai/text-embedding-3-large"}}
    try:
        build_inference_models_toml(
            _LLM, _RERANK, {**_TIERS, "embedding": "my-embed"}, embedding_models=embedding
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError: embedding model needs a 'dimension'")


def test_embedding_tier_must_be_defined():
    try:
        build_inference_models_toml(_LLM, _RERANK, {**_TIERS, "embedding": "ghost"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for undefined embedding tier model_ref")


def test_llm_tier_must_be_defined():
    try:
        build_inference_models_toml(_LLM, _RERANK, {**_TIERS, "standard": "ghost"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for undefined chat tier model_ref")


def test_rerank_tier_must_be_defined():
    try:
        build_inference_models_toml(_LLM, _RERANK, {**_TIERS, "rerank": "ghost"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for undefined rerank tier model_ref")


# --- Shared structural validation (headless JSON + interactive custom catalogs) --
# These exercise _validate_surface_catalog directly: it is the unit both operator
# paths call on their custom (untrusted) catalogs. It is intentionally NOT run
# inside build_inference_models_toml, so the shipped defaults are never re-checked.


def _expect_value_error(msg, surface, models):
    try:
        _validate_surface_catalog(surface, models)
    except ValueError:
        return
    raise AssertionError(msg)


def test_bad_api_style_rejected():
    llm = {"m": {**_LLM["gemini-3.1-flash-lite"], "api_style": "bogus"}}
    _expect_value_error("expected ValueError for invalid api_style", "llm", llm)


def test_chat_missing_required_field_rejected():
    # Drop 'provider' from a chat model -- the proxy schema requires it.
    broken = {k: v for k, v in _LLM["gemini-3.5-flash"].items() if k != "provider"}
    _expect_value_error(
        "expected ValueError for chat model missing 'provider'", "llm", {"m": broken}
    )


def test_non_int_numeric_field_rejected():
    llm = {"m": {**_LLM["gemini-3.5-flash"], "max_retries": "two"}}
    _expect_value_error("expected ValueError for non-integer max_retries", "llm", llm)


def test_pinecone_model_with_api_key_ref_rejected():
    rerank = {"bge-reranker-v2-m3": {**_RERANK["bge-reranker-v2-m3"], "api_key_ref": "nope"}}
    _expect_value_error(
        "expected ValueError for pinecone model carrying an api_key_ref", "rerank", rerank
    )


def test_litellm_rerank_with_api_version_rejected():
    rerank = {
        "my-rr": {"api_style": "litellm", "model": "cohere/rerank-v3.5", "api_version": "2024-01"}
    }
    _expect_value_error(
        "expected ValueError for litellm rerank carrying an api_version", "rerank", rerank
    )


def test_non_object_catalog_rejected():
    # Headless JSON that parses to a non-object (e.g. a list) must be rejected.
    _expect_value_error("expected ValueError for a non-object catalog", "llm", ["not", "a", "dict"])


def test_default_catalogs_pass_validation():
    # The shipped defaults must satisfy the same validator every operator catalog
    # is held to -- guards against future drift in the _DEFAULT_* tables. (These
    # are NOT validated at runtime; this is a CI-only consistency guard.)
    _validate_surface_catalog("llm", _DEFAULT_LLM_MODELS)
    _validate_surface_catalog("embedding", _DEFAULT_EMBEDDING_MODELS)
    _validate_surface_catalog("rerank", _DEFAULT_RERANK_MODELS)


# --- Per-surface independent customization --------------------------------


def test_all_surfaces_default_when_nothing_passed():
    # No catalog for any surface -> a complete TOML built entirely from shipped
    # defaults (chat + embedding + rerank), matching the static template's ids.
    parsed = tomllib.loads(build_inference_models_toml())
    assert set(parsed["llm_models"]) == {
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
    }
    assert "multilingual-e5-large" in parsed["embedding_models"]
    assert "bge-reranker-v2-m3" in parsed["rerank_models"]
    assert parsed["default"]["llm"]["tiers"]["standard"]["model_ref"] == "gemini-3.5-flash"
    assert parsed["default"]["rerank"]["tiers"]["default"]["model_ref"] == "bge-reranker-v2-m3"


def test_customize_embedding_only_keeps_default_chat_and_rerank():
    embedding = {
        "my-embed": {"api_style": "litellm", "model": "voyage/voyage-3", "dimension": 1024}
    }
    parsed = tomllib.loads(
        build_inference_models_toml(embedding_models=embedding, tiers={"embedding": "my-embed"})
    )
    # embedding is the operator's; chat + rerank fall back to shipped defaults.
    assert set(parsed["embedding_models"]) == {"my-embed"}
    assert parsed["default"]["embedding"]["tiers"]["default"]["model_ref"] == "my-embed"
    assert set(parsed["llm_models"]) == {
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
    }
    assert "bge-reranker-v2-m3" in parsed["rerank_models"]
    assert parsed["default"]["llm"]["tiers"]["lite"]["model_ref"] == "gemini-3.1-flash-lite"


def test_customize_chat_only_keeps_default_embedding_and_rerank():
    parsed = tomllib.loads(build_inference_models_toml(_LLM, tiers=_TIERS))
    assert set(parsed["llm_models"]) == set(_LLM)
    assert "multilingual-e5-large" in parsed["embedding_models"]
    assert "bge-reranker-v2-m3" in parsed["rerank_models"]
    assert parsed["default"]["rerank"]["tiers"]["default"]["model_ref"] == "bge-reranker-v2-m3"


def test_customize_rerank_only_keeps_default_chat_and_embedding():
    rerank = {"my-rerank": {"api_style": "litellm", "model": "cohere/rerank-v3.5"}}
    parsed = tomllib.loads(
        build_inference_models_toml(rerank_models=rerank, tiers={"rerank": "my-rerank"})
    )
    assert set(parsed["rerank_models"]) == {"my-rerank"}
    assert parsed["default"]["rerank"]["tiers"]["default"]["model_ref"] == "my-rerank"
    assert set(parsed["llm_models"]) == {
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
    }
    assert "multilingual-e5-large" in parsed["embedding_models"]


def test_customized_chat_without_tiers_raises():
    # Customizing chat but omitting the tier ids is a hard error (not a silent
    # fallback to the default chat tiers, which wouldn't reference these ids).
    try:
        build_inference_models_toml(_LLM)
    except ValueError:
        return
    raise AssertionError("expected ValueError when a customized chat surface has no tiers")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
