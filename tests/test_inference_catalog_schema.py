"""The shipped inference catalog must only use fields the proxy accepts.

nexus-inference-proxy forbids extra fields on a model, so a key it does not know
makes it reject its whole config at startup. That surfaces as crashlooping proxy
pods and a failed `helm --wait` -- after the cluster is built, with the cause one
pydantic error in a pod log. Three shipped artifacts can carry such a key: the
TOML template written into the generated project, the `_DEFAULT_*_MODELS` dicts
behind the headless path, and the interactive prompt field lists.

These tests hold all three to `_PROXY_MODEL_FIELDS`, so updating that one set
when the proxy changes is enough, and forgetting to fails here rather than on a
cell.

Run standalone (`python tests/test_inference_catalog_schema.py`) or under pytest.
"""

import os
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

import wizard  # noqa: E402

_SURFACE_TABLE = {
    "llm": "llm_models",
    "embedding": "embedding_models",
    "rerank": "rerank_models",
}


def _template_models() -> dict[str, dict]:
    """The model tables from the static TOML default installs write verbatim."""
    parsed = tomllib.loads(wizard.NEXUS_INFERENCE_MODELS_TEMPLATE)
    return {s: parsed.get(table, {}) for s, table in _SURFACE_TABLE.items()}


def test_toml_template_uses_only_proxy_fields():
    for surface, models in _template_models().items():
        allowed = wizard._PROXY_MODEL_FIELDS[surface]
        for model_id, fields in models.items():
            unknown = sorted(set(fields) - allowed)
            assert not unknown, f"{surface} model {model_id!r} carries {unknown}"


def test_default_model_dicts_use_only_proxy_fields():
    for surface, models in (
        ("llm", wizard._DEFAULT_LLM_MODELS),
        ("embedding", wizard._DEFAULT_EMBEDDING_MODELS),
        ("rerank", wizard._DEFAULT_RERANK_MODELS),
    ):
        allowed = wizard._PROXY_MODEL_FIELDS[surface]
        for model_id, fields in models.items():
            unknown = sorted(set(fields) - allowed)
            assert not unknown, f"{surface} default {model_id!r} carries {unknown}"


def test_prompted_int_fields_are_proxy_fields():
    """A prompt for a field the proxy rejects builds an unbootable config."""
    for surface, names in wizard._SURFACE_INT_FIELDS.items():
        unknown = sorted(set(names) - wizard._PROXY_MODEL_FIELDS[surface])
        assert not unknown, f"{surface} prompts for {unknown}"


def test_template_survives_the_wizard_validator():
    for surface, models in _template_models().items():
        wizard._validate_surface_catalog(surface, models)


def test_validator_rejects_an_unknown_field():
    models = {
        "m": {
            "api_style": "litellm",
            "model": "gemini/x",
            "label": "X",
            "provider": "gemini",
            "context_window": 1000,
            "not_a_proxy_field": 2,
        }
    }
    try:
        wizard._validate_surface_catalog("llm", models)
    except ValueError as e:
        assert "not_a_proxy_field" in str(e), e
        return
    raise AssertionError("expected ValueError naming the unaccepted field")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
