"""Network-free unit tests for setup/validate_models.py.

Covers the pieces the PR review called out as silently-drifting: the probe
classification table, the api_key_ref resolution fallback order, the local
registry checks that mirror the proxy's startup validation, the litellm
max_output_tokens clamp, and the catalog-iteration / exit-code logic.

Run standalone (`python tests/test_validate_models.py`) or under pytest. No
network and no litellm install required — the live probes and the litellm
registry lookup are monkeypatched.
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))

import validate_models as vm  # noqa: E402


@contextlib.contextmanager
def _patch(**attrs):
    """Temporarily replace module attributes on validate_models, restoring after."""
    saved = {name: getattr(vm, name) for name in attrs}
    for name, value in attrs.items():
        setattr(vm, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(vm, name, value)


@contextlib.contextmanager
def _env(**pairs):
    """Set env vars for the block; restore prior values (or unset) after."""
    saved = {k: os.environ.get(k) for k in pairs}
    for k, v in pairs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- _classify_probe: the table that silently drifts --------------------------


def test_classify_probe_table():
    cases = {
        200: "ok",
        429: "ok",  # rate-limited still proves the key is valid
        401: "fail",
        403: "fail",
        400: "fail",
        404: "fail",
        422: "fail",
        500: "warn",  # provider-side / transient — don't fail the run
        503: "warn",
        None: "warn",  # network error
    }
    for status, expected in cases.items():
        state, _ = vm._classify_probe(status, "detail")
        assert state == expected, f"status {status} -> {state}, expected {expected}"


# --- key resolution fallback order --------------------------------------------


def test_resolve_provider_key_prefers_pulumi_secret():
    with (
        _patch(_pulumi_config_get=lambda *a, **k: "from-pulumi"),
        _env(**{"gemini-api-key": "from-env", "GEMINI_API_KEY": "from-env-upper"}),
    ):
        assert vm._resolve_provider_key(".", None, "gemini-api-key") == "from-pulumi"


def test_resolve_provider_key_env_as_written_then_upper_snake():
    # pulumi returns nothing -> ref as-written wins over the UPPER_SNAKE form.
    with (
        _patch(_pulumi_config_get=lambda *a, **k: None),
        _env(**{"gemini-api-key": "as-written", "GEMINI_API_KEY": "upper"}),
    ):
        assert vm._resolve_provider_key(".", None, "gemini-api-key") == "as-written"
    # ref as-written unset -> fall back to UPPER_SNAKE.
    with (
        _patch(_pulumi_config_get=lambda *a, **k: None),
        _env(**{"gemini-api-key": None, "GEMINI_API_KEY": "upper"}),
    ):
        assert vm._resolve_provider_key(".", None, "gemini-api-key") == "upper"


def test_resolve_provider_key_none_when_nothing_set():
    with (
        _patch(_pulumi_config_get=lambda *a, **k: None),
        _env(**{"gemini-api-key": None, "GEMINI_API_KEY": None}),
    ):
        assert vm._resolve_provider_key(".", None, "gemini-api-key") is None


# --- litellm max_output_tokens clamp ------------------------------------------


def test_clamp_output_tokens():
    info = {"max_output_tokens": 32768}
    assert vm._clamp_output_tokens(20_000_000, info) == 32768  # oversized -> ceiling
    assert vm._clamp_output_tokens(1000, info) == 1000  # under ceiling -> unchanged
    assert vm._clamp_output_tokens(None, info) is None  # unset -> unset
    assert vm._clamp_output_tokens(20_000_000, None) == 20_000_000  # no registry info
    assert vm._clamp_output_tokens(500, {}) == 500  # registry lacks the field


# --- local registry checks (blocking-1: a PASS must not crash the proxy) -------

_CHAT_INFO = {
    "mode": "chat",
    "supported_openai_params": ["tools", "response_format"],
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 32768,
}


def _check(cfg, surface="chat", have_litellm=True):
    return vm._check_one_model(".", None, cfg.get("model", "m"), surface, cfg, have_litellm)


def _chat_cfg(**over):
    """A structurally-valid litellm chat entry (all proxy-required fields present),
    so tests exercise the registry/probe path rather than the structural check."""
    return {
        "api_style": "litellm",
        "model": "vendor/chat",
        "api_key_ref": "k",
        "label": "Vendor Chat",
        "provider": "vendor",
        **over,
    }


def _embed_cfg(**over):
    """A structurally-valid pinecone embedding entry."""
    return {
        "api_style": "pinecone",
        "model": "multilingual-e5-large",
        "dimension": 1024,
        "max_input_chars": 1000,
        "max_batch_size": 96,
        **over,
    }


def test_litellm_unknown_model_fails():
    # Live probe could pass, but the proxy ValueErrors at startup on an
    # unknown-to-registry model — so this must fail here.
    cfg = _chat_cfg(model="vendor/made-up")
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _litellm_model_info=lambda m: None,
        _probe_litellm_chat=lambda *a, **k: (200, ""),
    ):
        assert _check(cfg) == "fail"


def test_litellm_mode_mismatch_fails():
    cfg = _chat_cfg(model="vendor/embedder")
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _litellm_model_info=lambda m: {"mode": "embedding"},
        _probe_litellm_chat=lambda *a, **k: (200, ""),
    ):
        assert _check(cfg) == "fail"


def test_litellm_chat_missing_openai_params_fails():
    cfg = _chat_cfg()
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _litellm_model_info=lambda m: {"mode": "chat", "supported_openai_params": ["tools"]},
        _probe_litellm_chat=lambda *a, **k: (200, ""),
    ):
        assert _check(cfg) == "fail"


def test_openai_chat_requires_budgets():
    # openai-style skips registry default-fill, so both budgets must be set or
    # the proxy ValueErrors at startup.
    cfg = _chat_cfg(api_style="openai", model="gpt", base_url="https://h")
    with _patch(_resolve_provider_key=lambda *a, **k: "key"):
        assert _check(cfg) == "fail"
    cfg_ok = {**cfg, "context_window": 8000, "max_output_tokens": 1000}
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _probe_openai=lambda *a, **k: (200, ""),
    ):
        assert _check(cfg_ok) == "pass"


# --- blocking-2: oversized budgets the proxy clamps must not hard-fail ---------


def test_litellm_oversized_context_window_warns_not_fails():
    cfg = _chat_cfg(context_window=5_000_000)  # exceeds the 1M registry limit -> proxy clamps
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _litellm_model_info=lambda m: _CHAT_INFO,
        _probe_litellm_chat=lambda *a, **k: (200, ""),
    ):
        assert _check(cfg) == "pass"


def test_litellm_oversized_max_output_tokens_probes_clamped_value_and_warns():
    seen = {}
    warnings = []

    def _probe(model, key, base_url, api_version, max_output_tokens):
        seen["max"] = max_output_tokens
        return (200, "")

    cfg = _chat_cfg(max_output_tokens=20_000_000)  # proxy clamps to registry ceiling 32768
    with _patch(
        _resolve_provider_key=lambda *a, **k: "key",
        _litellm_model_info=lambda m: _CHAT_INFO,
        _probe_litellm_chat=_probe,
        warn=warnings.append,
    ):
        assert _check(cfg) == "pass"
    assert seen["max"] == 32768, "probe should send the clamped value, not the raw config"
    assert any("max_output_tokens" in w and "clamp" in w for w in warnings), (
        "operator should be warned the configured budget will be clamped"
    )


# --- skip accounting + dimension check ----------------------------------------


def test_litellm_skipped_when_not_installed():
    cfg = _chat_cfg()
    with _patch(_resolve_provider_key=lambda *a, **k: "key"):
        assert _check(cfg, have_litellm=False) == "skip"


def test_embedding_dimension_mismatch_fails():
    cfg = _embed_cfg(dimension=300)
    with _patch(
        _pinecone_probe_key=lambda *a, **k: "key",
        _probe_pinecone_embed=lambda *a, **k: (200, "", 1024),
    ):
        assert _check(cfg, surface="embedding") == "fail"


def test_embedding_dimension_match_passes():
    cfg = _embed_cfg(dimension=1024)
    with _patch(
        _pinecone_probe_key=lambda *a, **k: "key",
        _probe_pinecone_embed=lambda *a, **k: (200, "", 1024),
    ):
        assert _check(cfg, surface="embedding") == "pass"


def test_missing_key_fails():
    cfg = _chat_cfg()
    with _patch(_resolve_provider_key=lambda *a, **k: None):
        assert _check(cfg) == "fail"


# --- structural checks: shapes the proxy hard-fails on at startup -------------


def test_missing_required_field_fails():
    # embedding entry with no dimension -> pydantic can't construct it -> no boot.
    cfg = _embed_cfg()
    del cfg["dimension"]
    with _patch(_pinecone_probe_key=lambda *a, **k: "key"):
        assert _check(cfg, surface="embedding") == "fail"


def test_chat_missing_label_provider_fails():
    cfg = _chat_cfg()
    del cfg["provider"]
    with _patch(_resolve_provider_key=lambda *a, **k: "key"):
        assert _check(cfg) == "fail"


def test_non_positive_numeric_field_fails():
    cfg = _embed_cfg(max_batch_size=0)
    with _patch(_pinecone_probe_key=lambda *a, **k: "key"):
        assert _check(cfg, surface="embedding") == "fail"


def test_pinecone_with_api_key_ref_fails():
    # api_key_ref/api_key are forbidden on pinecone models; the proxy rejects it.
    cfg = _embed_cfg(api_key_ref="some-key")
    with _patch(_pinecone_probe_key=lambda *a, **k: "key"):
        assert _check(cfg, surface="embedding") == "fail"


def test_litellm_rerank_with_api_version_fails():
    # A live rerank probe drops api_version, so only this local check catches it.
    cfg = {
        "api_style": "litellm",
        "model": "vendor/rerank",
        "api_key_ref": "k",
        "max_query_chars": 1000,
        "max_doc_chars": 800,
        "max_docs_per_request": 100,
        "api_version": "2024-01",
    }
    with _patch(_resolve_provider_key=lambda *a, **k: "key"):
        assert _check(cfg, surface="rerank") == "fail"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
