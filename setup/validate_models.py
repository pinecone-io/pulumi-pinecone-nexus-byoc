#!/usr/bin/env python3
"""Validate a Nexus BYOC inference catalog — run it when YOU choose, after setup.

The setup wizard writes `inference-proxy-models.toml` and stores your provider
keys as stack secrets, but it does NOT contact any model. This optional script
does: for every model in the catalog it fires one tiny live request (the same
call the inference-proxy makes) so a bad or expired key, a wrong `base_url`, a
misspelled `model` id, an out-of-scope token budget, a missing `api_version`, or
an embedding-dimension mismatch is caught here instead of at runtime.

This is only a lightweight *sanity ping* — one minimal request per model. A pass
means the key/endpoint/model id/budgets are usable; it is NOT a guarantee that
real workloads (long contexts, tools, large batches) will behave correctly.

It is shipped into your generated project so you can run it from there:

    cd <your-project-dir>
    uv run --no-project --with 'litellm==1.87.0' --python 3.12 python validate_models.py
    # or point at another project:
    uv run --no-project --with 'litellm==1.87.0' --python 3.12 python validate_models.py --stack-dir ../pinecone-nexus-byoc

`--no-project` keeps this a quick ephemeral run (no rebuild of the project's
pulumi package); `--python 3.12` pins an interpreter new enough for `tomllib`
(`--no-project` ignores the project's requires-python). Pin litellm to the
version the inference-proxy runs (1.87.0) so
the local model registry and call behavior match the proxy; the script warns if a
different litellm is installed. `litellm` is only needed for `litellm`-style
models (Gemini, Azure, etc.); `openai`- and `pinecone`-style models validate with
the standard library alone.
Reading the provider-key secrets needs the stack to be decryptable — set
`PULUMI_CONFIG_PASSPHRASE` (local backend) or be logged in (Pulumi Cloud).

Exit code is 0 when every model is valid, 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

MODELS_FILENAME = "inference-proxy-models.toml"

# Mirrors nexus-inference-proxy defaults (adapters/pinecone.py, settings.py).
_DEFAULT_PINECONE_BASE_URL = "https://api.pinecone.io"
_DEFAULT_PINECONE_API_VERSION = "2025-10"

# The litellm version the inference-proxy is built with (nexus-inference-proxy/
# pyproject.toml). Validate with the SAME version so the local model registry
# (get_model_info: known-model, mode, token limits) and the SDK's per-provider
# call behavior match what the proxy will actually run. Keep in sync with the
# deployed Nexus version.
_EXPECTED_LITELLM_VERSION = "1.87.0"

# Minimum Python for the `uv run --no-project` invocation. `--no-project` ignores
# the project's requires-python, and this script needs `tomllib` (3.11+), so the
# printed command pins an interpreter explicitly. Bootstrap extracts this value.
_EXPECTED_PYTHON_VERSION = "3.12"

# The faithful way to run this: litellm pinned to the proxy's version, on an
# interpreter new enough for tomllib. Single source for every "re-run with ..."
# hint below (and mirrored by bootstrap.sh's printed command).
_RECOMMENDED_RUN = (
    f"uv run --no-project --with 'litellm=={_EXPECTED_LITELLM_VERSION}' "
    f"--python {_EXPECTED_PYTHON_VERSION} python validate_models.py"
)

# (TOML table, surface label / litellm mode) for the three model surfaces.
_MODEL_TABLES = (
    ("llm_models", "chat"),
    ("embedding_models", "embedding"),
    ("rerank_models", "rerank"),
)

# Fields the proxy's settings schema declares with NO default (nexus-inference-proxy
# settings.py: LLM/Embedding/RerankModelDefinition). Pydantic fails model
# construction if any is absent, so the proxy won't boot — but a live probe never
# exercises them. Keep in sync with the surface model definitions.
_REQUIRED_FIELDS = {
    "chat": ("model", "api_style", "label", "provider"),
    "embedding": ("model", "api_style", "dimension", "max_input_chars", "max_batch_size"),
    "rerank": ("model", "api_style", "max_query_chars", "max_doc_chars", "max_docs_per_request"),
}

# Fields the proxy requires to be positive integers (rejected at startup if <= 0).
_POSITIVE_INT_FIELDS = {
    "chat": (),  # context_window / max_output_tokens are optional (checked in-probe)
    "embedding": ("dimension", "max_input_chars", "max_batch_size"),
    "rerank": ("max_query_chars", "max_doc_chars", "max_docs_per_request"),
}

_GREEN, _RED, _YELLOW, _DIM, _BOLD, _RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)


def ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_RESET} {msg}")


def fail(msg: str, hint: str | None = None) -> None:
    print(f"  {_RED}✗{_RESET} {msg}")
    if hint:
        print(f"    {_DIM}{hint}{_RESET}")


def warn(msg: str) -> None:
    print(f"  {_YELLOW}⚠{_RESET} {msg}")


# Resolved `pulumi config get` values cached per (stack_dir, stack, key, path) —
# the same api_key_ref is shared by many models and each call shells out (up to
# 30 s), so without this a large catalog re-decrypts the same secret repeatedly.
_CONFIG_CACHE: dict[tuple, str | None] = {}


def _pulumi_on_path() -> bool:
    return shutil.which("pulumi") is not None


def _pulumi_config_get(
    stack_dir: str, key: str, stack: str | None, path: bool = False
) -> str | None:
    """One stack config value. `pulumi config get` decrypts a secret to plaintext
    by default (needs the stack passphrase / cloud login) — there is no
    --show-secrets flag on `get` (that's only for `pulumi config` list view).

    Returns None if the value is unset/undecryptable OR pulumi isn't installed;
    the caller distinguishes the latter via `_pulumi_on_path()` so a missing CLI
    isn't misreported as a missing secret."""
    if not _pulumi_on_path():
        return None
    cache_key = (stack_dir, stack, key, path)
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]
    args = ["pulumi", "config", "get"]
    if path:
        args.append("--path")
    args += [key, "--cwd", stack_dir]
    if stack:
        args += ["--stack", stack]
    value = None
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            value = r.stdout.strip()
    except Exception:
        pass
    _CONFIG_CACHE[cache_key] = value
    return value


def _resolve_provider_key(stack_dir: str, stack: str | None, ref: str) -> str | None:
    """Value for an ``api_key_ref``: the ``nexus-provider-keys.<ref>`` stack secret,
    else the env fallback the proxy uses (ref as-written, then UPPER_SNAKE)."""
    return (
        _pulumi_config_get(stack_dir, f"nexus-provider-keys.{ref}", stack, path=True)
        or os.environ.get(ref)
        or os.environ.get(ref.upper().replace("-", "_"))
    )


def _pinecone_probe_key(stack_dir: str, stack: str | None) -> str | None:
    """Pinecone-style models take the caller's ``Api-Key`` per request; probe with
    the deployment key (``pinecone-api-key`` stack secret, else PINECONE_API_KEY)."""
    return _pulumi_config_get(stack_dir, "pinecone-api-key", stack) or os.environ.get(
        "PINECONE_API_KEY"
    )


def _classify_probe(status: int | None, detail: str) -> tuple[str, str]:
    """Map a probe outcome to ("ok" | "warn" | "fail", reason)."""
    if status == 200:
        return ("ok", "reachable, key accepted")
    if status in (401, 403):
        return ("fail", f"auth rejected ({status}) — bad or unauthorized key")
    if status in (400, 404, 422):
        return ("fail", f"rejected ({status}) — wrong model id, base_url, or params: {detail}")
    if status == 429:
        return ("ok", "rate-limited (429) — key is valid")
    if isinstance(status, int) and 500 <= status < 600:
        return ("warn", f"provider error ({status}); could not verify: {detail}")
    return ("warn", f"could not reach provider: {detail}")


def _http_post(
    url: str, headers: dict, body: dict, timeout: int = 20
) -> tuple[int | None, str, str]:
    """POST JSON. Returns (status, error-detail, response-body-text)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200, "", resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):
            detail = e.read().decode("utf-8", "replace")[:200]
        return (e.code, detail, "")
    except Exception as e:
        return (None, str(e), "")


def _import_litellm():
    """Import litellm with its own console chatter silenced — the feedback banner,
    the "LiteLLM.Info" hints, and its ERROR-level logging — so only our per-model
    lines show. The exception objects we classify are unaffected."""
    import litellm

    litellm.suppress_debug_info = True  # drops the "Give Feedback / Get Help" + Info lines
    with contextlib.suppress(Exception):
        import logging

        for _name in ("LiteLLM", "litellm"):
            logging.getLogger(_name).setLevel(logging.CRITICAL)
    return litellm


def _litellm_model_info(model: str) -> dict | None:
    """The model's entry in LiteLLM's local registry, or None if litellm doesn't
    recognize it. The proxy calls ``litellm.get_model_info`` at startup for every
    litellm-style model (settings.py `_get_litellm_model_info`) and raises when it
    isn't found, so an unknown model here means the proxy would fail to boot — the
    live probe can still pass (litellm routes by provider prefix without registry
    info), which is exactly the false-pass this replicates."""
    litellm = _import_litellm()

    try:
        info = litellm.get_model_info(model=model)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def _clamp_output_tokens(max_output_tokens: int | None, info: dict | None) -> int | None:
    """The max_tokens the proxy would actually send for a litellm-style chat model:
    the configured budget clamped down to the registry ceiling (settings.py
    `_finalize_llm_model` + adapters `apply_max_tokens_ceiling`). Probing with the
    raw configured value would false-fail a budget the proxy silently clamps."""
    ceiling = (info or {}).get("max_output_tokens")
    if max_output_tokens and isinstance(ceiling, int) and ceiling > 0:
        return min(max_output_tokens, ceiling)
    return max_output_tokens


def _probe_litellm_chat(
    model, api_key, base_url, api_version, max_output_tokens
) -> tuple[int | None, str]:
    litellm = _import_litellm()

    kwargs = dict(
        model=model,
        api_key=api_key,
        num_retries=0,
        drop_params=True,
        timeout=20,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=max_output_tokens or 1,  # send the configured output budget
    )
    if base_url:
        kwargs["base_url"] = base_url
    if api_version:
        kwargs["api_version"] = api_version
    try:
        litellm.completion(**kwargs)
        return (200, "")
    except Exception as e:
        return (getattr(e, "status_code", None), str(e)[:200])


def _probe_litellm_embed(
    model, api_key, base_url, api_version
) -> tuple[int | None, str, int | None]:
    litellm = _import_litellm()

    kwargs = dict(model=model, api_key=api_key, num_retries=0, drop_params=True, timeout=20)
    if base_url:
        kwargs["api_base"] = base_url  # embed uses api_base, not base_url
    if api_version:
        kwargs["api_version"] = api_version
    try:
        resp = litellm.embedding(input=["ping"], **kwargs)
        dim = None
        with contextlib.suppress(Exception):
            data = resp["data"] if isinstance(resp, dict) else resp.data
            dim = len(data[0]["embedding"])
        return (200, "", dim)
    except Exception as e:
        return (getattr(e, "status_code", None), str(e)[:200], None)


def _probe_litellm_rerank(model, api_key, base_url) -> tuple[int | None, str]:
    litellm = _import_litellm()

    kwargs = dict(model=model, api_key=api_key, num_retries=0, drop_params=True, timeout=20)
    if base_url:
        kwargs["api_base"] = base_url  # rerank takes no api_version
    try:
        litellm.rerank(query="ping", documents=["doc"], return_documents=False, **kwargs)
        return (200, "")
    except Exception as e:
        return (getattr(e, "status_code", None), str(e)[:200])


def _probe_openai(
    model, api_key, base_url, api_version, max_output_tokens
) -> tuple[int | None, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    if api_version:  # Azure-style ?api-version= query param, as the proxy sends it
        url += ("&" if "?" in url else "?") + "api-version=" + api_version
    status, detail, _ = _http_post(
        url,
        {"Authorization": f"Bearer {api_key}"},
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": max_output_tokens or 1,
        },
    )
    return (status, detail)


def _pinecone_headers(api_key, api_version) -> dict:
    return {
        "Api-Key": api_key,
        "X-Pinecone-API-Version": api_version or _DEFAULT_PINECONE_API_VERSION,
    }


def _probe_pinecone_embed(
    model, api_key, base_url, api_version
) -> tuple[int | None, str, int | None, bool]:
    """Returns (status, detail, dim, is_sparse). A sparse model answers with
    `sparse_values`/`sparse_indices` instead of `values` — Nexus embedding needs a
    dense model, so the caller turns is_sparse into a fail."""
    base = (base_url or _DEFAULT_PINECONE_BASE_URL).rstrip("/")
    status, detail, text = _http_post(
        f"{base}/embed",
        _pinecone_headers(api_key, api_version),
        {
            "model": model,
            "inputs": [{"text": "ping"}],
            "parameters": {"input_type": "passage", "truncate": "END"},
        },
    )
    dim = None
    is_sparse = False
    if status == 200:
        with contextlib.suppress(Exception):
            item = json.loads(text)["data"][0]
            if "values" in item:
                dim = len(item["values"])
            elif "sparse_values" in item or "sparse_indices" in item:
                is_sparse = True
    return (status, detail, dim, is_sparse)


def _probe_pinecone_rerank(model, api_key, base_url, api_version) -> tuple[int | None, str]:
    base = (base_url or _DEFAULT_PINECONE_BASE_URL).rstrip("/")
    status, detail, _ = _http_post(
        f"{base}/rerank",
        _pinecone_headers(api_key, api_version),
        {
            "model": model,
            "query": "ping",
            "documents": [{"text": "doc"}],
            "return_documents": False,
            "parameters": {"truncate": "END"},
        },
    )
    return (status, detail)


def _structural_error(surface: str, api_style, cfg: dict) -> str | None:
    """Config-shape problems the proxy hard-fails on at startup but a live probe
    never exercises. Returns an error message, or None if the shape is sound.
    Mirrors the per-model pydantic validators in nexus-inference-proxy settings.py."""
    missing = [f for f in _REQUIRED_FIELDS[surface] if not cfg.get(f)]
    if missing:
        return (
            f"missing/empty required field(s) {missing} — the proxy's settings schema "
            "requires them and won't start without them"
        )
    for f in _POSITIVE_INT_FIELDS[surface]:
        v = cfg.get(f)
        if not isinstance(v, int) or v <= 0:
            return f"{f} must be a positive integer (got {v!r}); the proxy rejects it at startup"
    if api_style == "pinecone" and (cfg.get("api_key_ref") or cfg.get("api_key")):
        return (
            "api_key_ref/api_key are not allowed for api_style='pinecone' — Pinecone "
            "credentials are supplied per request via the Api-Key header (proxy fails at startup)"
        )
    if surface == "rerank" and api_style == "litellm" and cfg.get("api_version"):
        return (
            "api_version is only valid for api_style='pinecone' rerank; litellm.rerank has no "
            "such parameter and the proxy rejects it at startup"
        )
    return None


def _check_one_model(stack_dir, stack, model_id, surface, cfg, have_litellm) -> str:
    """Validate one catalog entry. Returns "pass", "fail", or "skip"
    (litellm-style model but litellm isn't installed — nothing was checked)."""
    label = f"{surface} '{model_id}'"
    api_style = cfg.get("api_style")
    model = cfg.get("model", model_id)
    base_url = cfg.get("base_url")
    api_version = cfg.get("api_version")
    context_window = cfg.get("context_window")
    max_output_tokens = cfg.get("max_output_tokens")
    dimension = cfg.get("dimension")

    # 0) config-shape checks the proxy hard-fails on at startup but a live probe
    # can't exercise (missing/empty required fields, non-positive budgets, forbidden
    # field combos). Reported before the probe so a broken shape fails cheaply.
    structural = _structural_error(surface, api_style, cfg)
    if structural:
        fail(f"{label}: {structural}")
        return "fail"

    # 1) resolve the key we'll probe with.
    if api_style == "pinecone":
        key = _pinecone_probe_key(stack_dir, stack)
        if not key:
            fail(
                f"{label}: no Pinecone API key to probe with",
                "set PINECONE_API_KEY or the pinecone-api-key stack secret",
            )
            return "fail"
    elif api_style in ("litellm", "openai"):
        ref = cfg.get("api_key_ref")
        if not ref:
            fail(f"{label}: no api_key_ref set (litellm/openai models need one)")
            return "fail"
        key = _resolve_provider_key(stack_dir, stack, ref)
        if not key:
            hint = (
                f"pulumi config set --path --secret nexus-provider-keys.{ref} <key> "
                "(and ensure PULUMI_CONFIG_PASSPHRASE / backend login so it decrypts)"
            )
            if not _pulumi_on_path():
                hint = (
                    f"pulumi is not on PATH, so the stack secret can't be read; set env {ref} "
                    f"(or {ref.upper().replace('-', '_')}), or install pulumi and re-run"
                )
            fail(f"{label}: provider key '{ref}' not set or not decryptable", hint)
            return "fail"
    else:
        fail(f"{label}: unknown api_style {api_style!r} (expected litellm / openai / pinecone)")
        return "fail"

    # 2) preconditions for issuing the request.
    if api_style == "litellm" and not have_litellm:
        warn(
            f"{label}: litellm not installed; skipping "
            f"(re-run with: uv run --no-project --with 'litellm=={_EXPECTED_LITELLM_VERSION}' ...)"
        )
        return "skip"
    if api_style == "openai" and not base_url:
        fail(f"{label}: openai api_style requires base_url to send a request")
        return "fail"

    # 2a) LOCAL registry checks the proxy hard-fails on at startup (settings.py) but
    # a live ping doesn't exercise — replicate them so a "✓" here can't still
    # crash-loop the proxy after `pulumi up`. All are free (litellm is loaded).
    info = None
    if api_style == "litellm":
        info = _litellm_model_info(model)
        if info is None:
            fail(
                f"{label}: litellm's registry doesn't recognize {model!r} — the proxy "
                "calls litellm.get_model_info() at startup and would fail to boot",
                "use a model id litellm knows (check the LiteLLM model list), or fix `model`",
            )
            return "fail"
        mode = info.get("mode")
        if mode != surface:  # surface is "chat" | "embedding" | "rerank" — the litellm modes
            fail(f"{label}: litellm reports mode={mode!r}, but this surface needs mode={surface!r}")
            return "fail"
        if surface == "chat":
            missing = {"tools", "response_format"} - set(info.get("supported_openai_params") or [])
            if missing:
                fail(
                    f"{label}: model lacks OpenAI params Nexus relies on: {sorted(missing)} "
                    "(the proxy rejects this at startup)"
                )
                return "fail"
    elif (
        surface == "chat"
        and api_style == "openai"
        and (not context_window or not max_output_tokens)
    ):
        # openai-style skips registry default-fill (_finalize_llm_model returns early),
        # so both budgets must be set explicitly or the proxy ValueErrors at startup.
        missing = [
            n
            for n, v in (
                ("context_window", context_window),
                ("max_output_tokens", max_output_tokens),
            )
            if not v
        ]
        fail(
            f"{label}: openai-style chat model must set {', '.join(missing)} "
            "(the proxy doesn't default-fill these for openai style and fails at startup)"
        )
        return "fail"

    # 2b) context_window isn't a request param. For litellm-style the proxy CLAMPS an
    # oversized context_window down to the registry input limit ("operator can
    # tighten, not loosen") rather than rejecting it — so surface it as a warning,
    # not a failure. The model still passes.
    if surface == "chat" and api_style == "litellm" and context_window:
        max_in = (info or {}).get("max_input_tokens")
        if isinstance(max_in, int) and max_in > 0 and context_window > max_in:
            warn(
                f"{label}: context_window {context_window} exceeds the model's input limit "
                f"{max_in}; the proxy will clamp it to {max_in}"
            )

    # max_output_tokens IS a request param, so the probe below sends the clamped
    # value (see _clamp_output_tokens) to avoid a false fail — but the operator
    # should still know their configured budget will be capped, same as above.
    if surface == "chat" and api_style == "litellm" and max_output_tokens:
        max_out = (info or {}).get("max_output_tokens")
        if isinstance(max_out, int) and max_out > 0 and max_output_tokens > max_out:
            warn(
                f"{label}: max_output_tokens {max_output_tokens} exceeds the model's output "
                f"limit {max_out}; the proxy will clamp it to {max_out}"
            )

    # 3) the tiny live request. For openai-style, max_output_tokens is sent raw as
    # max_tokens (the proxy doesn't clamp there) so an out-of-range budget fails
    # live. For litellm-style the proxy clamps max_output_tokens down to the
    # registry ceiling BEFORE the request, so probe with the clamped value — sending
    # the raw oversized value would be a false positive (see PR review).
    observed_dim = None
    pinecone_sparse = False
    if surface == "chat":
        if api_style == "litellm":
            status, detail = _probe_litellm_chat(
                model,
                key,
                base_url,
                api_version,
                _clamp_output_tokens(max_output_tokens, info),
            )
        else:
            status, detail = _probe_openai(model, key, base_url, api_version, max_output_tokens)
    elif surface == "embedding":
        if api_style == "litellm":
            status, detail, observed_dim = _probe_litellm_embed(model, key, base_url, api_version)
        else:
            status, detail, observed_dim, pinecone_sparse = _probe_pinecone_embed(
                model, key, base_url, api_version
            )
    else:  # rerank
        if api_style == "litellm":
            status, detail = _probe_litellm_rerank(model, key, base_url)
        else:
            status, detail = _probe_pinecone_rerank(model, key, base_url, api_version)

    state, reason = _classify_probe(status, detail)
    if state == "fail":
        fail(f"{label}: {reason}")
        return "fail"
    if state == "warn":
        warn(f"{label}: {reason}")
        return "pass"  # network / 5xx — don't fail the run on a transient condition

    # 4a) the model answered as sparse (sparse_values/sparse_indices) — Nexus
    # embedding needs dense vectors, and an index built on it would mismatch.
    if pinecone_sparse:
        fail(
            f"{label}: {model!r} returned a sparse embedding (sparse_values/sparse_indices); "
            "Nexus embedding requires a dense model"
        )
        return "fail"

    # 4) embedding: the returned vector width must match the declared dimension.
    if (
        surface == "embedding"
        and dimension
        and observed_dim is not None
        and observed_dim != dimension
    ):
        fail(
            f"{label}: dimension mismatch — config declares {dimension}, "
            f"but the model returned {observed_dim}"
        )
        return "fail"

    extra = (
        f"; dimension {observed_dim} matches"
        if surface == "embedding" and dimension and observed_dim
        else ""
    )
    ok(f"{label}: {reason}{extra}")
    return "pass"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Nexus BYOC inference-model catalog (local checks + live probe).",
    )
    parser.add_argument(
        "--stack-dir",
        default=".",
        help=f"generated project dir holding {MODELS_FILENAME} (default: current dir)",
    )
    parser.add_argument(
        "--stack",
        help="Pulumi stack name (default: the project's currently-selected stack)",
    )
    args = parser.parse_args()

    print()
    print(f"  {_BOLD}Nexus inference-model validation{_RESET}")
    print()

    models_path = os.path.join(args.stack_dir, MODELS_FILENAME)
    if not os.path.isfile(models_path):
        warn(
            f"No {MODELS_FILENAME} in {args.stack_dir} — nothing to validate "
            "(is this a Nexus-enabled project?)."
        )
        return 0
    try:
        with open(models_path, "rb") as f:
            catalog = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        fail(f"Could not parse {MODELS_FILENAME}: {e}")
        return 1

    try:
        _import_litellm()

        have_litellm = True
        import importlib.metadata as _md

        with contextlib.suppress(Exception):
            installed = _md.version("litellm")
            if installed != _EXPECTED_LITELLM_VERSION:
                warn(
                    f"litellm {installed} is installed, but the inference-proxy runs litellm "
                    f"{_EXPECTED_LITELLM_VERSION} — its model registry/behavior may differ, so "
                    f"results here may not match the proxy. For a faithful check: {_RECOMMENDED_RUN}"
                )
    except ImportError:
        have_litellm = False
        warn(
            "litellm not installed — litellm-style models will be skipped "
            f"(re-run with: {_RECOMMENDED_RUN} ...)"
        )

    failed = skipped = passed = 0
    saw_model = False
    for table, surface in _MODEL_TABLES:
        for model_id, cfg in (catalog.get(table) or {}).items():
            saw_model = True
            if not isinstance(cfg, dict):
                fail(f"{surface} '{model_id}': malformed model table")
                failed += 1
                continue
            status = _check_one_model(
                args.stack_dir, args.stack, model_id, surface, cfg, have_litellm
            )
            failed += status == "fail"
            skipped += status == "skip"
            passed += status == "pass"

    print()
    if not saw_model:
        warn("Catalog defines no models.")
        return 0
    if failed:
        fail("One or more inference models are invalid — fix the config above before `pulumi up`.")
        return 1
    # No failures. A run that validated nothing (all litellm-style, litellm absent)
    # must not read as a clean pass — surface the skip count and how to cover it.
    if skipped:
        skip_note = f"re-run with '{_RECOMMENDED_RUN}' to validate them"
        if passed:
            warn(
                f"{passed} model(s) passed; {skipped} skipped (litellm not installed) — {skip_note}."
            )
        else:
            warn(
                f"Nothing validated — all {skipped} model(s) skipped (litellm not installed). {skip_note}."
            )
        return 0
    ok("All inference models passed.")
    print(
        f"    {_DIM}Note: this is only a lightweight sanity ping (one tiny request per "
        f"model) — it confirms the key, endpoint and model id.{_RESET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
