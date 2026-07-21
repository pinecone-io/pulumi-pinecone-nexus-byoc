"""Guards the curate-window contract: supported_curate_models ⊆ supported_llm_models.

Run standalone (`python tests/test_inference_models_toml.py`) or under pytest.
"""

import os
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

from wizard import NEXUS_INFERENCE_MODELS_TEMPLATE, build_inference_models_toml  # noqa: E402

_LLM = {
    "gemini-3.1-flash-lite": {"api_style": "litellm", "model": "gemini/gemini-3.1-flash-lite"},
    "gemini-3.5-flash": {"api_style": "litellm", "model": "gemini/gemini-3.5-flash"},
    "gemini-3.1-pro-preview": {"api_style": "litellm", "model": "gemini/gemini-3.1-pro-preview"},
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
    llm = {**_LLM, "gemini-3.5-flash-preview": {"api_style": "litellm", "model": "x"}}
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
