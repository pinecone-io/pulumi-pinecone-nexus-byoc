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
    uv run --no-project --with 'litellm==1.87.0' python validate_models.py   # from the project dir
    # or point at another project:
    uv run --no-project --with 'litellm==1.87.0' python validate_models.py --stack-dir ../pinecone-nexus-byoc

`--no-project` keeps this a quick ephemeral run (no rebuild of the project's
pulumi package). Pin litellm to the version the inference-proxy runs (1.87.0) so
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

# (TOML table, surface label / litellm mode) for the three model surfaces.
_MODEL_TABLES = (
    ("llm_models", "chat"),
    ("embedding_models", "embedding"),
    ("rerank_models", "rerank"),
)

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


def _pulumi_config_get(stack_dir: str, key: str, stack: str | None, path: bool = False) -> str | None:
    """One stack config value. `pulumi config get` decrypts a secret to plaintext
    by default (needs the stack passphrase / cloud login) — there is no
    --show-secrets flag on `get` (that's only for `pulumi config` list view)."""
    args = ["pulumi", "config", "get"]
    if path:
        args.append("--path")
    args += [key, "--cwd", stack_dir]
    if stack:
        args += ["--stack", stack]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


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


def _http_post(url: str, headers: dict, body: dict, timeout: int = 20) -> tuple[int | None, str, str]:
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


def _litellm_input_limit(model: str) -> int | None:
    """The model's max input tokens per LiteLLM's registry (local; None if unknown).

    Used only to validate ``context_window`` — the sole budget field that isn't a
    request parameter, so a live ping can't exercise it.
    """
    litellm = _import_litellm()

    try:
        return litellm.get_model_info(model=model).get("max_input_tokens")
    except Exception:
        return None


def _probe_litellm_chat(model, api_key, base_url, api_version, max_output_tokens) -> tuple[int | None, str]:
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


def _probe_litellm_embed(model, api_key, base_url, api_version) -> tuple[int | None, str, int | None]:
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


def _probe_openai(model, api_key, base_url, api_version, max_output_tokens) -> tuple[int | None, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    if api_version:  # Azure-style ?api-version= query param, as the proxy sends it
        url += ("&" if "?" in url else "?") + "api-version=" + api_version
    status, detail, _ = _http_post(
        url,
        {"Authorization": f"Bearer {api_key}"},
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": max_output_tokens or 1},
    )
    return (status, detail)


def _pinecone_headers(api_key, api_version) -> dict:
    return {"Api-Key": api_key, "X-Pinecone-API-Version": api_version or _DEFAULT_PINECONE_API_VERSION}


def _probe_pinecone_embed(model, api_key, base_url, api_version) -> tuple[int | None, str, int | None]:
    base = (base_url or _DEFAULT_PINECONE_BASE_URL).rstrip("/")
    status, detail, text = _http_post(
        f"{base}/embed",
        _pinecone_headers(api_key, api_version),
        {"model": model, "inputs": [{"text": "ping"}], "parameters": {"input_type": "passage", "truncate": "END"}},
    )
    dim = None
    if status == 200:
        with contextlib.suppress(Exception):
            dim = len(json.loads(text)["data"][0]["values"])
    return (status, detail, dim)


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


def _check_one_model(stack_dir, stack, model_id, surface, cfg, have_litellm) -> bool:
    label = f"{surface} '{model_id}'"
    api_style = cfg.get("api_style")
    model = cfg.get("model", model_id)
    base_url = cfg.get("base_url")
    api_version = cfg.get("api_version")
    context_window = cfg.get("context_window")
    max_output_tokens = cfg.get("max_output_tokens")
    dimension = cfg.get("dimension")

    # 1) resolve the key we'll probe with.
    if api_style == "pinecone":
        key = _pinecone_probe_key(stack_dir, stack)
        if not key:
            fail(f"{label}: no Pinecone API key to probe with",
                 "set PINECONE_API_KEY or the pinecone-api-key stack secret")
            return False
    elif api_style in ("litellm", "openai"):
        ref = cfg.get("api_key_ref")
        if not ref:
            fail(f"{label}: no api_key_ref set (litellm/openai models need one)")
            return False
        key = _resolve_provider_key(stack_dir, stack, ref)
        if not key:
            fail(f"{label}: provider key '{ref}' not set or not decryptable",
                 f"pulumi config set --path --secret nexus-provider-keys.{ref} <key> "
                 "(and ensure PULUMI_CONFIG_PASSPHRASE / backend login so it decrypts)")
            return False
    else:
        fail(f"{label}: unknown api_style {api_style!r} (expected litellm / openai / pinecone)")
        return False

    # 2) preconditions for issuing the request.
    if api_style == "litellm" and not have_litellm:
        warn(f"{label}: litellm not installed; skipping "
             f"(re-run with: uv run --no-project --with 'litellm=={_EXPECTED_LITELLM_VERSION}' ...)")
        return True
    if api_style == "openai" and not base_url:
        fail(f"{label}: openai api_style requires base_url to send a request")
        return False

    # 2b) context_window isn't a request param, so validate it against the model's
    # real input limit (LiteLLM registry — litellm chat models only).
    if surface == "chat" and api_style == "litellm" and context_window:
        max_in = _litellm_input_limit(model)
        if isinstance(max_in, int) and context_window > max_in:
            fail(f"{label}: context_window {context_window} exceeds the model's input limit {max_in}")
            return False

    # 3) the tiny live request. max_output_tokens (if set) is sent as max_tokens, so
    # an out-of-range output budget fails at the provider.
    observed_dim = None
    if surface == "chat":
        if api_style == "litellm":
            status, detail = _probe_litellm_chat(model, key, base_url, api_version, max_output_tokens)
        else:
            status, detail = _probe_openai(model, key, base_url, api_version, max_output_tokens)
    elif surface == "embedding":
        if api_style == "litellm":
            status, detail, observed_dim = _probe_litellm_embed(model, key, base_url, api_version)
        else:
            status, detail, observed_dim = _probe_pinecone_embed(model, key, base_url, api_version)
    else:  # rerank
        if api_style == "litellm":
            status, detail = _probe_litellm_rerank(model, key, base_url)
        else:
            status, detail = _probe_pinecone_rerank(model, key, base_url, api_version)

    state, reason = _classify_probe(status, detail)
    if state == "fail":
        fail(f"{label}: {reason}")
        return False
    if state == "warn":
        warn(f"{label}: {reason}")
        return True  # network / 5xx — don't fail the run on a transient condition

    # 4) embedding: the returned vector width must match the declared dimension.
    if surface == "embedding" and dimension and observed_dim is not None and observed_dim != dimension:
        fail(f"{label}: dimension mismatch — config declares {dimension}, "
             f"but the model returned {observed_dim}")
        return False

    extra = f"; dimension {observed_dim} matches" if surface == "embedding" and observed_dim else ""
    ok(f"{label}: {reason}{extra}")
    return True


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
        warn(f"No {MODELS_FILENAME} in {args.stack_dir} — nothing to validate "
             "(is this a Nexus-enabled project?).")
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
                warn(f"litellm {installed} is installed, but the inference-proxy runs litellm "
                     f"{_EXPECTED_LITELLM_VERSION} — its model registry/behavior may differ, so "
                     "results here may not match the proxy. For a faithful check: "
                     f"uv run --no-project --with 'litellm=={_EXPECTED_LITELLM_VERSION}' python validate_models.py")
    except ImportError:
        have_litellm = False
        warn("litellm not installed — litellm-style models will be skipped "
             f"(re-run with: uv run --no-project --with 'litellm=={_EXPECTED_LITELLM_VERSION}' python validate_models.py ...)")

    all_ok = True
    saw_model = False
    for table, surface in _MODEL_TABLES:
        for model_id, cfg in (catalog.get(table) or {}).items():
            saw_model = True
            if not isinstance(cfg, dict):
                fail(f"{surface} '{model_id}': malformed model table")
                all_ok = False
                continue
            all_ok = _check_one_model(args.stack_dir, args.stack, model_id, surface, cfg, have_litellm) and all_ok

    print()
    if not saw_model:
        warn("Catalog defines no models.")
        return 0
    if all_ok:
        ok("All inference models passed.")
        print(f"    {_DIM}Note: this is only a lightweight sanity ping (one tiny request per "
              f"model) — it confirms the key, endpoint and model id.{_RESET}")
        return 0
    fail("One or more inference models are invalid — fix the config above before `pulumi up`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
