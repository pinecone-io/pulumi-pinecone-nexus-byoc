"""Pinecone BYOC setup wizard."""

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TypedDict

# shared UNIQUE preflight checks -- single source of truth, also used by the
# standalone dev/preflight.py so the two never drift. First-party module in
# this same directory (added to sys.path when run as a script / by preflight.py).
import preflight_checks
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.status import Status

IS_WINDOWS = platform.system() == "Windows"
if not IS_WINDOWS:
    import termios
    import tty


# pinecone blue
BLUE = "#002BFF"


class NexusWizardConfig(TypedDict, total=False):
    """Wizard-side Nexus answers, threaded from the prompts/headless env into
    `_generate_project`. Distinct from the package's runtime `NexusConfig`
    (emitted in the generated `__main__.py`). `total=False`: a DB-only install
    is just `{"enabled": False}`; the rest are present only when Nexus is on.
    `inference_models_toml` is `None` when no catalog was built (the default
    template is written instead).
    """

    enabled: bool
    byoc_env: str
    byoc_project_id: str
    storage_bucket_prefix: str
    nexus_version: str
    image_registry: str
    inference_base: str
    inference_models_toml: str | None
    # Gemini API key (the default catalog's `gemini-api-key` ref). Collected by
    # the wizard so it can set the `nexus-gemini-api-key` /
    # `nexus-provider-keys.gemini-api-key` secrets itself.
    gemini_api_key: str
    # Extra provider-key secrets for a customized catalog whose api_key_refs are
    # not `gemini-api-key`: {ref -> value}, each set as `nexus-provider-keys.<ref>`.
    provider_keys: dict[str, str]


# Canonical UUID form (e.g. 123e4567-e89b-12d3-a456-426614174000). The Nexus BYOC
# project id is the Pinecone gCPS project UUID (matched against `projects.id` by
# CPGW), NOT the GCP project name -- so it must validate as a UUID.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


# GCS bucket-name prefix for Nexus storage. The cluster provisions
# `{prefix}-source`, `{prefix}-knowledge`, `{prefix}-archive`; the longest
# suffix is `-knowledge` (10 chars). GCS bucket names (and the derived DNS
# labels) must stay <= 63 chars, so the prefix itself must be <= 53. Bucket
# names are lowercase letters/digits/hyphens and must start and end with an
# alphanumeric character.
_STORAGE_PREFIX_MAX_LEN = 53
_STORAGE_PREFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _is_storage_bucket_prefix(value: str) -> bool:
    value = value.strip()
    if not value or len(value) > _STORAGE_PREFIX_MAX_LEN:
        return False
    return bool(_STORAGE_PREFIX_RE.match(value))


# `api_key_ref = "<ref>"` lines in an inference-models TOML. Used to discover
# which provider-key secrets a (possibly customized) catalog needs.
_API_KEY_REF_RE = re.compile(r'^\s*api_key_ref\s*=\s*"([^"]+)"', re.MULTILINE)


def _api_key_refs_from_toml(toml_text: str | None) -> set[str]:
    """Distinct `api_key_ref` values referenced by an inference-models TOML.

    `None` (no custom catalog -> default template) references only the default
    `gemini-api-key`.
    """
    if toml_text is None:
        return {"gemini-api-key"}
    return set(_API_KEY_REF_RE.findall(toml_text))


# TODO: Temp from https://github.com/pinecone-io/pinecone-db/pull/16775
PINECONE_VERSION = "avi-fdb-in-byoc-d596c27"
NEXUS_VERSION = "main-d13ff4f"

# Nexus images live in their own `nexus` repo, co-located on the registry host;
# DB/pinetools images stay in the `unstable` repo.
NEXUS_IMAGE_REGISTRY = "us-docker.pkg.dev/pinecone-artifacts/nexus"
NEXUS_AZURE_IMAGE_REGISTRY = "pinecone.azurecr.io/nexus"

# Inference-proxy model-routing template written into the generated project when
# Nexus is enabled. It's layered onto the proxy's baked default as the `byoc`
# config profile; the customer edits it, then `pulumi up` ships it as a
# ConfigMap. Gemini + Pinecone only (the keys a BYOC deploy reliably has) and a
# complete `default` profile -- project/phase overrides are intentionally out of
# scope. Keep every api_key_ref's secret wired via `nexus-provider-keys.<ref>`.
NEXUS_INFERENCE_MODELS_TEMPLATE = """\
# Inference models for this BYOC deployment.
#
# These are the models the deployment serves and how the lite / standard / pro
# (chat) and default (embedding / rerank) tiers route to them. Edit to taste,
# then run `pulumi up`. This config fully defines the catalog -- only the models
# listed here are served.
#
# For every `api_key_ref` below, set its secret value (the wizard printed the
# exact commands):
#   pulumi config set --path --secret nexus-provider-keys.<api-key-ref> <value>
# Pinecone embed/rerank models need NO api_key_ref -- the caller supplies the
# key per request via the Api-Key header.
#
# Keep it complete: chat tiers lite/standard/pro, plus one embedding default and
# one rerank default. Each tier's model_ref must be one of the ids defined below
# (and listed in the matching supported_*_models).

# --- Model catalog -------------------------------------------------------
[llm_models."gemini-3.1-flash-lite"]
api_style      = "litellm"
model          = "gemini/gemini-3.1-flash-lite"
api_key_ref    = "gemini-api-key"
label          = "Gemini 3.1 Flash Lite"
provider       = "gemini"
max_retries    = 2
context_window = 1_000_000

[llm_models."gemini-3.5-flash"]
api_style      = "litellm"
model          = "gemini/gemini-3.5-flash"
api_key_ref    = "gemini-api-key"
label          = "Gemini 3.5 Flash"
provider       = "gemini"
max_retries    = 2
context_window = 1_000_000

[llm_models."gemini-3.1-pro-preview"]
api_style      = "litellm"
model          = "gemini/gemini-3.1-pro-preview"
api_key_ref    = "gemini-api-key"
label          = "Gemini 3.1 Pro"
provider       = "gemini"
max_retries    = 5
context_window = 1_000_000

# FIXED -- do not change. The embedding model is locked platform-wide (the
# nexus index dimension is frozen to it); pointing the embedding tier elsewhere
# fails deploy validation.
[embedding_models.multilingual-e5-large]
api_style       = "pinecone"
model           = "multilingual-e5-large"
max_retries     = 2
max_input_chars = 1000
max_batch_size  = 96

[rerank_models.bge-reranker-v2-m3]
api_style            = "pinecone"
model                = "bge-reranker-v2-m3"
max_retries          = 2
max_query_chars      = 1000
max_doc_chars        = 800
max_docs_per_request = 100

# --- Default profile (the deployment baseline) ---------------------------
[default]
supported_llm_models       = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.1-pro-preview"]
supported_embedding_models = ["multilingual-e5-large"]
supported_rerank_models    = ["bge-reranker-v2-m3"]
supported_curate_models    = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]

[default.llm.tiers.lite]
model_ref = "gemini-3.1-flash-lite"

[default.llm.tiers.standard]
model_ref = "gemini-3.5-flash"

[default.llm.tiers.pro]
model_ref = "gemini-3.1-pro-preview"

[default.embedding.tiers.default]
model_ref = "multilingual-e5-large"

[default.rerank.tiers.default]
model_ref = "bge-reranker-v2-m3"

# Override the image default's search phase, which otherwise inherits
# claude-sonnet-4-6 (not in this deployment's gemini-only supported models).
[default.llm.phase_defaults]
search = "gemini-3.5-flash"
"""

# Filename of the routing overlay written into the generated project.
NEXUS_INFERENCE_MODELS_FILENAME = "inference-proxy-models.toml"

# Header comment for wizard/headless-built model TOML (mirrors the template).
_INFERENCE_MODELS_HEADER = """\
# Inference models for this BYOC deployment (generated by the setup wizard).
#
# These are the chat (lite/standard/pro) and rerank models the deployment
# serves. Edit to taste, then run `pulumi up`. This config fully defines the
# catalog -- only the models listed here are served.
#
# The embedding model is FIXED platform-wide (the nexus index dimension is
# frozen to it) and is injected automatically -- it is not operator-configurable.
#
# For every `api_key_ref` below, set its secret value:
#   pulumi config set --path --secret nexus-provider-keys.<api-key-ref> <value>
# Pinecone embed/rerank models need NO api_key_ref (caller supplies it per request).
"""

LLM_MODEL_TIERS = ("lite", "standard", "pro")

# Embedding is fixed platform-wide: every embedding tier must resolve to this
# exact pinecone model or the proxy refuses to start (the nexus index dimension
# is frozen to it -- see nexus-inference-proxy EXPECTED_EMBEDDING_MODEL). The
# wizard never prompts for it; it's injected into every built catalog here.
LOCKED_EMBEDDING_MODEL_ID = "multilingual-e5-large"
_LOCKED_EMBEDDING_TABLE = (
    f'[embedding_models."{LOCKED_EMBEDDING_MODEL_ID}"]\n'
    'api_style       = "pinecone"\n'
    f'model           = "{LOCKED_EMBEDDING_MODEL_ID}"\n'
    "max_retries     = 2\n"
    "max_input_chars = 1000\n"
    "max_batch_size  = 96\n"
)


def _toml_scalar(v) -> str:
    """Serialize a scalar (str / bool / int) as a TOML value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"unsupported TOML scalar type for value {v!r}")


def _emit_model_table(map_name: str, model_id: str, fields: dict) -> str:
    # Quote the id key: catalog ids legitimately contain dots / slashes / hyphens.
    lines = [f'[{map_name}."{model_id}"]']
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    return "\n".join(lines)


def build_inference_models_toml(
    llm_models: dict[str, dict],
    rerank_models: dict[str, dict],
    tiers: dict[str, str],
) -> str:
    """Render a complete inference-routing TOML from operator-chosen catalogs.

    Only chat + rerank are operator-configurable. ``tiers`` keys:
    ``lite`` / ``standard`` / ``pro`` (llm ids) and ``rerank`` (a rerank id).
    The embedding model is fixed platform-wide and injected here (it is not a
    parameter). ``supported_<surface>_models`` is auto-set to every defined id.
    The clean-slate sentinel is NOT emitted here -- it's injected at deploy time
    by the Nexus component so this file stays purely about models.
    """
    if not llm_models or not rerank_models:
        raise ValueError("llm and rerank must each have at least one model")

    # The proxy refuses to boot (assert_deploy_complete, nexus#1083) when any two
    # chat tiers resolve to the same model_ref -- the tier-as-alias contract
    # requires them distinct. Catch it here rather than at CrashLoop.
    base_tiers = {t: tiers[t] for t in LLM_MODEL_TIERS}
    if len(set(base_tiers.values())) < len(base_tiers):
        raise ValueError(
            f"chat tiers {'/'.join(LLM_MODEL_TIERS)} must each map to a distinct model "
            f"(got {base_tiers}); the inference proxy rejects colliding tiers"
        )

    parts: list[str] = [_INFERENCE_MODELS_HEADER, "# --- Model catalog ---"]
    for model_id, fields in llm_models.items():
        parts.append(_emit_model_table("llm_models", model_id, fields))
    # Embedding is fixed -- inject it rather than taking it from the operator.
    parts.append("# Embedding is fixed platform-wide and managed by Pinecone BYOC.")
    parts.append(_LOCKED_EMBEDDING_TABLE.rstrip())
    for model_id, fields in rerank_models.items():
        parts.append(_emit_model_table("rerank_models", model_id, fields))

    supported_llm = "[" + ", ".join(_toml_scalar(i) for i in llm_models) + "]"
    supported_rr = "[" + ", ".join(_toml_scalar(i) for i in rerank_models) + "]"
    # LLM catalog minus the pro tier. Emitted so it overrides the image base's broader list and stays a subset.
    curate_ids = [i for i in llm_models if i != tiers["pro"]]
    supported_curate = "[" + ", ".join(_toml_scalar(i) for i in curate_ids) + "]"
    parts.append(
        "# --- Default profile (supported_* = every model defined above) ---\n"
        "[default]\n"
        f"supported_llm_models = {supported_llm}\n"
        f"supported_embedding_models = [{_toml_scalar(LOCKED_EMBEDDING_MODEL_ID)}]\n"
        f"supported_rerank_models = {supported_rr}\n"
        f"supported_curate_models = {supported_curate}\n\n"
        + "".join(
            f"[default.llm.tiers.{t}]\nmodel_ref = {_toml_scalar(tiers[t])}\n\n"
            for t in LLM_MODEL_TIERS
        )
        + f"[default.embedding.tiers.default]\nmodel_ref = {_toml_scalar(LOCKED_EMBEDDING_MODEL_ID)}\n\n"
        f"[default.rerank.tiers.default]\nmodel_ref = {_toml_scalar(tiers['rerank'])}\n\n"
        "# Override the image default's search phase, which otherwise inherits\n"
        "# claude-sonnet-4-6 (not in this deployment's supported models).\n"
        f"[default.llm.phase_defaults]\nsearch = {_toml_scalar(tiers['standard'])}"
    )
    return "\n\n".join(parts) + "\n"


console = Console()


@dataclass
class PreflightResult:
    name: str
    passed: bool
    message: str
    details: str | None = None


def _read_input_with_placeholder_unix(
    prompt: str, placeholder: str = "", password: bool = False
) -> str:
    console.print(f"  {prompt}: ", end="")

    # open /dev/tty directly to handle curl pipe case where stdin is not a TTY
    # use binary mode with no buffering to avoid input lag
    tty_file = open("/dev/tty", "rb", buffering=0)
    try:
        fd = tty_file.fileno()
        old_settings = termios.tcgetattr(fd)
    except Exception:
        tty_file.close()
        raise

    def show_placeholder():
        if placeholder and not password:
            sys.stdout.write(f"\033[2m{placeholder}\033[0m")  # dim
            sys.stdout.write(f"\033[{len(placeholder)}D")  # move back
            sys.stdout.flush()

    def clear_placeholder():
        if placeholder and not password:
            sys.stdout.write(" " * len(placeholder))
            sys.stdout.write(f"\033[{len(placeholder)}D")
            sys.stdout.flush()

    show_placeholder()

    try:
        tty.setraw(fd)
        result = []
        placeholder_visible = True

        while True:
            char = tty_file.read(1).decode("utf-8", errors="replace")

            # enter - accept
            if char in ("\r", "\n"):
                if not result and placeholder:
                    result = list(placeholder)
                break

            # tab or right arrow - complete with placeholder
            if char == "\t" or char == "\x1b":
                if char == "\x1b":
                    # read arrow key sequence
                    next1 = tty_file.read(1).decode("utf-8", errors="replace")
                    next2 = tty_file.read(1).decode("utf-8", errors="replace")
                    if next1 == "[" and next2 == "C" and placeholder and not result:  # right arrow
                        clear_placeholder()
                        result = list(placeholder)
                        sys.stdout.write(placeholder)
                        sys.stdout.flush()
                        placeholder_visible = False
                    continue
                else:  # tab
                    if placeholder and not result:
                        clear_placeholder()
                        result = list(placeholder)
                        sys.stdout.write(placeholder)
                        sys.stdout.flush()
                        placeholder_visible = False
                    continue

            # backspace
            if char in ("\x7f", "\x08"):
                if result:
                    result.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                    # if empty, show placeholder again
                    if not result and placeholder and not password:
                        show_placeholder()
                        placeholder_visible = True
                continue

            # ctrl+c
            if char == "\x03":
                raise KeyboardInterrupt

            # ctrl+d
            if char == "\x04":
                if not result:
                    raise EOFError
                continue

            # ignore other control chars
            if ord(char) < 32:
                continue

            # clear placeholder on first real char
            if placeholder_visible and placeholder and not password:
                clear_placeholder()
                placeholder_visible = False

            result.append(char)
            sys.stdout.write("•" if password else char)
            sys.stdout.flush()

        return "".join(result)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        tty_file.close()
        console.print()


def _read_input_with_placeholder_windows(
    prompt: str, placeholder: str = "", password: bool = False
) -> str:
    if sys.platform != "win32":
        return placeholder or ""
    import msvcrt

    console.print(f"  {prompt}: ", end="")

    def show_placeholder():
        if placeholder and not password:
            sys.stdout.write(f"\033[2m{placeholder}\033[0m")  # dim
            sys.stdout.write(f"\033[{len(placeholder)}D")  # move back
            sys.stdout.flush()

    def clear_placeholder():
        if placeholder and not password:
            sys.stdout.write(" " * len(placeholder))
            sys.stdout.write(f"\033[{len(placeholder)}D")
            sys.stdout.flush()

    show_placeholder()

    result = []
    placeholder_visible = True

    try:
        while True:
            if msvcrt.kbhit():
                char_bytes = msvcrt.getch()

                # tab or right arrow - complete with placeholder
                if char_bytes in (b"\x00", b"\xe0"):
                    special = msvcrt.getch()
                    if (
                        char_bytes == b"\xe0" and special == b"M" and placeholder and not result
                    ):  # right arrow
                        clear_placeholder()
                        result = list(placeholder)
                        sys.stdout.write(placeholder)
                        sys.stdout.flush()
                        placeholder_visible = False
                    continue

                try:
                    char = char_bytes.decode("utf-8", errors="replace")
                except Exception:
                    continue

                # enter - accept
                if char == "\r":
                    if not result and placeholder:
                        result = list(placeholder)
                    break

                # tab or right arrow - complete with placeholder
                if char == "\t":
                    if placeholder and not result:
                        clear_placeholder()
                        result = list(placeholder)
                        sys.stdout.write(placeholder)
                        sys.stdout.flush()
                        placeholder_visible = False
                    continue

                # backspace
                if char in ("\x08", "\x7f"):
                    if result:
                        result.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                        # if empty, show placeholder again
                        if not result and placeholder and not password:
                            show_placeholder()
                            placeholder_visible = True
                    continue

                # ctrl+c
                if char == "\x03":
                    raise KeyboardInterrupt

                # ctrl+d
                if char == "\x04":
                    if not result:
                        raise EOFError
                    continue

                # ignore other control chars
                if ord(char) < 32:
                    continue

                # clear placeholder on first real char
                if placeholder_visible and placeholder and not password:
                    clear_placeholder()
                    placeholder_visible = False

                result.append(char)
                sys.stdout.write("•" if password else char)
                sys.stdout.flush()

        return "".join(result)

    finally:
        console.print()


def _read_input_with_placeholder(prompt: str, placeholder: str = "", password: bool = False) -> str:
    if IS_WINDOWS:
        return _read_input_with_placeholder_windows(prompt, placeholder, password)
    else:
        return _read_input_with_placeholder_unix(prompt, placeholder, password)


# ---------------------------------------------------------------------------
# Base Setup Wizard (shared between AWS and GCP)
# ---------------------------------------------------------------------------


class BaseSetupWizard:
    TOTAL_STEPS = 13
    CLOUD_NAME: str = ""
    HEADER_TITLE: str = "Pinecone BYOC Setup Wizard"
    HEADER_SUBTITLE: str = "This wizard will set up everything you need to deploy Pinecone BYOC."
    DEFAULT_CIDR: str = "10.0.0.0/16"
    CIDR_DESC: str = "The IP range for your VPC (must not conflict with existing VPCs)"
    DELETION_PROTECTION_DESC: str = ""
    PRIVATE_ACCESS_DESC: str = ""
    METADATA_NAME: str = "tags"

    def __init__(
        self,
        headless: bool = False,
        stack_name: str = "prod",
        skip_install: bool = False,
        project_name: str | None = None,
        local_package_path: str | None = None,
    ):
        self.results: list[PreflightResult] = []
        self._current_step = 0
        self._headless = headless
        self._stack_name = stack_name
        self._skip_install = skip_install
        self._project_name = project_name
        self._local_package_path = local_package_path

    def _step(self, title: str) -> str:
        self._current_step += 1
        return f"[{BLUE}]Step {self._current_step}/{self.TOTAL_STEPS}[/] · {title}"

    def _prompt(self, message: str, default: str | None = None, password: bool = False) -> str:
        return _read_input_with_placeholder(message, default or "", password)

    def _print_header(self):
        console.print()
        console.print(
            Panel.fit(
                f"[bold {BLUE}]{self.HEADER_TITLE}[/]",
                border_style=BLUE,
                padding=(0, 2),
            )
        )
        console.print()
        console.print(f"  {self.HEADER_SUBTITLE}", style="dim")
        console.print()

    def _get_api_key(self) -> str | None:
        console.print()
        console.print(f"  {self._step('Pinecone API Key')}")
        console.print("  [dim]Find your key at app.pinecone.io[/]")
        console.print()

        env_key = os.environ.get("PINECONE_API_KEY")
        if env_key:
            use_env = self._prompt("Found PINECONE_API_KEY in environment. Use it? (Y/n)", "Y")
            if use_env.lower() in ("y", "yes", ""):
                return env_key

        api_key = self._prompt("Enter your Pinecone API key", password=True)
        if not api_key:
            console.print("\n  [red]✗[/] API key is required")
            return None

        return api_key

    def _get_gemini_api_key(self) -> str:
        """The Gemini API key backing the default catalog's `gemini-api-key` ref; required when Nexus is enabled."""
        console.print()
        console.print("  [bold]Gemini API Key[/]")
        console.print("  [dim]Nexus uses Gemini for curation and the default inference models.[/]")
        console.print("  [dim]Get a key at aistudio.google.com/apikey[/]")
        console.print()

        for env_var in ("PINECONE_GEMINI_API_KEY", "GEMINI_API_KEY"):
            env_key = os.environ.get(env_var)
            if env_key:
                use_env = self._prompt(f"Found {env_var} in environment. Use it? (Y/n)", "Y")
                if use_env.lower() in ("y", "yes", ""):
                    return env_key

        while True:
            gemini_key = self._prompt("Enter your Gemini API key", password=True).strip()
            if gemini_key:
                return gemini_key
            console.print("  [red]Gemini API key is required for Nexus.[/]")

    def _validate_api_key(self, api_key: str) -> bool:
        console.print()
        console.print(f"  {self._step('Validating API Key')}")
        console.print()

        import urllib.error
        import urllib.request

        with Status("  [dim]Checking API key...[/]", console=console, spinner="dots"):
            try:
                req = urllib.request.Request(
                    "https://api.pinecone.io/indexes",
                    headers={"Api-Key": api_key},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    response.read()

                console.print("  [green]✓[/] API key is valid")
                return True
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    console.print("  [red]✗[/] Invalid API key")
                else:
                    console.print(f"  [red]✗[/] API error: {e.code}")
                return False
            except Exception as e:
                console.print(f"  [red]✗[/] Failed to validate API key: {e}")
                return False

    def _get_cidr(self) -> str:
        console.print()
        console.print(f"  {self._step('VPC CIDR Block')}")
        console.print(f"  [dim]{self.CIDR_DESC}[/]")
        console.print()
        return self._prompt("Enter CIDR block", self.DEFAULT_CIDR)

    def _get_deletion_protection(self) -> bool:
        console.print()
        console.print(f"  {self._step('Deletion Protection')}")
        console.print(f"  [dim]{self.DELETION_PROTECTION_DESC}[/]")
        console.print()
        response = self._prompt("Enable deletion protection? (Y/n)", "Y")
        return response.lower() in ("y", "yes", "")

    def _get_public_access(self) -> bool:
        console.print()
        console.print(f"  {self._step('Network Access')}")
        console.print("  [dim]Public access allows connections from the internet[/]")
        console.print(f"  [dim]{self.PRIVATE_ACCESS_DESC}[/]")
        console.print()
        response = self._prompt("Enable public access? (Y/n)", "Y")
        return response.lower() in ("y", "yes", "")

    def _get_custom_metadata(self) -> dict[str, str]:
        name = self.METADATA_NAME
        console.print()
        console.print(f"  {self._step(f'Resource {name.title()}')}")
        console.print(
            f"  [dim]Add custom {name} to all {self.CLOUD_NAME} resources (for cost tracking, etc.)[/]"
        )
        console.print("  [dim]Format: key=value, comma-separated (e.g., team=platform,env=prod)[/]")
        console.print()

        input_val = self._prompt(f"Enter {name} (or press Enter to skip)", "")
        if not input_val:
            return {}

        metadata = {}
        for pair in input_val.split(","):
            pair = pair.strip()
            if "=" in pair:
                key, value = pair.split("=", 1)
                metadata[key.strip()] = value.strip()

        if metadata:
            console.print(f"  [dim]{name.title()} to apply: {metadata}[/]")

        return metadata

    # ----- Inference model catalog (interactive guided entry) -------------

    def _prompt_int(self, message: str, default: int | None = None) -> int:
        while True:
            raw = self._prompt(message, "" if default is None else str(default)).strip()
            try:
                return int(raw)
            except ValueError:
                console.print("  [red]Enter a whole number.[/]")

    def _choose_from(self, message: str, options: list[str]) -> str:
        """Prompt the user to pick one id from `options` (defaults to the first)."""
        console.print(f"  [dim]Available: {', '.join(options)}[/]")
        while True:
            choice = self._prompt(message, options[0]).strip()
            if choice in options:
                return choice
            console.print(f"  [red]Pick one of: {', '.join(options)}[/]")

    def _collect_inference_models(self) -> str | None:
        """Guided catalog entry + tier selection -> routing TOML.

        Returns None to fall back to the default (Gemini + Pinecone) template.
        The clean-slate sentinel is injected later by the Nexus component, so the
        operator only ever deals with models and tiers here.
        """
        console.print()
        console.print(
            "  [dim]Define the chat + rerank models this deployment serves, or use"
            " the default Gemini + Pinecone set. The embedding model is fixed"
            f" ({LOCKED_EMBEDDING_MODEL_ID}) and configured automatically.[/]"
        )
        if self._prompt("Customize inference models? (y/N)", "N").strip().lower() not in (
            "y",
            "yes",
        ):
            return None

        while True:
            llm = self._collect_surface_models("llm")
            if len(llm) >= len(LLM_MODEL_TIERS):
                break
            console.print(
                f"  [red]At least {len(LLM_MODEL_TIERS)} chat models are required:"
                f" {'/'.join(LLM_MODEL_TIERS)} must each map to a distinct model."
                " Add more.[/]"
            )
        rerank = self._collect_surface_models("rerank")

        console.print()
        console.print("  [dim]Now map the tiers to models you defined.[/]")
        while True:
            tiers = {t: self._choose_from(f"Chat '{t}' model", list(llm)) for t in LLM_MODEL_TIERS}
            if len(set(tiers.values())) == len(LLM_MODEL_TIERS):
                break
            console.print(
                f"  [red]{'/'.join(LLM_MODEL_TIERS)} must each map to a distinct model."
                " Please pick again.[/]"
            )
        tiers["rerank"] = self._choose_from("Rerank model", list(rerank))
        return build_inference_models_toml(llm, rerank, tiers)

    def _collect_surface_models(self, surface: str) -> dict[str, dict]:
        """Loop collecting >=1 model for one surface (llm / rerank)."""
        console.print()
        console.print(f"  {self._step(f'{surface.title()} models')}")
        models: dict[str, dict] = {}
        while True:
            verb = "another" if models else "a"
            ask = self._prompt(f"Add {verb} {surface} model? (Y/n)", "Y").strip().lower()
            if ask not in ("y", "yes", ""):
                if models:
                    return models
                console.print(f"  [red]At least one {surface} model is required.[/]")
                continue
            model_id = self._prompt("  Model id (catalog key, e.g. my-flash)").strip()
            if not model_id:
                console.print("  [red]Model id is required.[/]")
                continue
            if surface == "llm":
                models[model_id] = self._collect_llm_model()
            else:
                models[model_id] = self._collect_rerank_model()

    def _collect_llm_model(self) -> dict:
        console.print(
            "  [dim]api_style: 'litellm' (a model LiteLLM knows) or 'openai'"
            " (any OpenAI-compatible endpoint).[/]"
        )
        api_style = self._choose_from("  api_style", ["litellm", "openai"])
        if api_style == "litellm":
            console.print(
                "  [dim]model must match LiteLLM EXACTLY (e.g. gemini/gemini-2.5-flash,"
                " anthropic/claude-...). See https://models.litellm.ai/[/]"
            )
        fields: dict = {
            "api_style": api_style,
            "model": self._prompt("  model").strip(),
            "label": self._prompt("  label (shown in console)").strip(),
            "provider": self._prompt("  provider tag (gemini / claude / openai / ...)").strip(),
            "api_key_ref": self._prompt(
                "  api_key_ref (provider key env var, e.g. gemini-api-key)"
            ).strip(),
            "max_retries": self._prompt_int("  max_retries", 2),
        }
        if api_style == "openai":
            # base_url + token budgets are required for openai-compat (no LiteLLM
            # registry to infer them from).
            fields["base_url"] = self._prompt("  base_url (required)").strip()
            fields["context_window"] = self._prompt_int("  context_window (required)")
            fields["max_output_tokens"] = self._prompt_int("  max_output_tokens (required)")
        else:
            base_url = self._prompt(
                "  base_url (optional, Enter for LiteLLM's default endpoint)", ""
            ).strip()
            if base_url:
                fields["base_url"] = base_url
            cw = self._prompt(
                "  context_window (optional, Enter to let LiteLLM decide)", ""
            ).strip()
            if cw:
                fields["context_window"] = int(cw)
            mot = self._prompt("  max_output_tokens (optional, Enter to skip)", "").strip()
            if mot:
                fields["max_output_tokens"] = int(mot)
        return fields

    def _collect_rerank_model(self) -> dict:
        console.print(
            "  [dim]api_style: 'pinecone' (Pinecone-hosted, key supplied per request)"
            " or 'litellm'.[/]"
        )
        api_style = self._choose_from("  api_style", ["pinecone", "litellm"])
        fields: dict = {"api_style": api_style, "model": self._prompt("  model").strip()}
        if api_style == "litellm":
            fields["api_key_ref"] = self._prompt("  api_key_ref (provider key env var)").strip()
            base_url = self._prompt(
                "  base_url (optional, Enter for LiteLLM's default endpoint)", ""
            ).strip()
            if base_url:
                fields["base_url"] = base_url
        fields["max_retries"] = self._prompt_int("  max_retries", 2)
        fields["max_query_chars"] = self._prompt_int("  max_query_chars", 1000)
        fields["max_doc_chars"] = self._prompt_int("  max_doc_chars", 800)
        fields["max_docs_per_request"] = self._prompt_int("  max_docs_per_request", 100)
        return fields

    def _headless_inference_models_toml(self) -> str | None:
        """Build the routing TOML from env vars (headless mode), or None to use
        the default template when none are set.

        Only chat + rerank are operator-configurable (the embedding model is
        fixed platform-wide and injected automatically). Env contract (JSON object
        is id -> model-definition, fields exactly as in nexus-inference-proxy's
        [<surface>_models.<id>] tables):
          PINECONE_NEXUS_LLM_MODELS / _RERANK_MODELS   (JSON)
          PINECONE_NEXUS_LLM_LITE / _STANDARD / _PRO    (model id)
          PINECONE_NEXUS_RERANK_MODEL                   (model id)
        """
        if not os.environ.get("PINECONE_NEXUS_LLM_MODELS"):
            console.print(
                "  [dim]Inference models: using the default Gemini + Pinecone catalog."
                " To customize, set PINECONE_NEXUS_LLM_MODELS / _RERANK_MODELS"
                " (JSON id->definition) plus the tier ids PINECONE_NEXUS_LLM_LITE /"
                " _STANDARD / _PRO and PINECONE_NEXUS_RERANK_MODEL. The embedding"
                f" model is fixed ({LOCKED_EMBEDDING_MODEL_ID}).[/]"
            )
            return None
        try:
            llm = json.loads(os.environ["PINECONE_NEXUS_LLM_MODELS"])
            rerank = json.loads(os.environ["PINECONE_NEXUS_RERANK_MODELS"])
            tiers = {t: os.environ[f"PINECONE_NEXUS_LLM_{t.upper()}"] for t in LLM_MODEL_TIERS}
            tiers["rerank"] = os.environ["PINECONE_NEXUS_RERANK_MODEL"]
        except KeyError as exc:
            raise ValueError(
                f"PINECONE_NEXUS_LLM_MODELS is set but {exc} is missing -- set "
                "PINECONE_NEXUS_RERANK_MODELS and the tier ids "
                "PINECONE_NEXUS_LLM_{LITE,STANDARD,PRO} / PINECONE_NEXUS_RERANK_MODEL."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in PINECONE_NEXUS_*_MODELS: {exc}") from exc
        return build_inference_models_toml(llm, rerank, tiers)

    def _get_project_name(self) -> str:
        # already collected in bootstrap via --project-name; don't reprompt or consume a step
        if self._project_name:
            return self._project_name
        console.print()
        console.print(f"  {self._step('Project Name')}")
        console.print("  [dim]A short name for this deployment (e.g., 'pinecone-prod')[/]")
        console.print()
        default_name = os.path.basename(os.getcwd()) or "pinecone-nexus-byoc"
        return self._prompt("Pulumi project name", default_name)

    def _setup_pulumi_backend(self) -> bool:
        console.print()
        console.print(f"  {self._step('Pulumi Backend')}")
        console.print("  [dim]Where to store infrastructure state[/]")
        console.print()

        backend = self._prompt("Backend (local/cloud)", "local").lower()
        use_local = backend != "cloud"

        if use_local:
            console.print()
            console.print("  [dim]Enter a passphrase to encrypt secrets (remember this!)[/]")
            passphrase = self._prompt("Passphrase", password=True)
            if not passphrase:
                console.print("  [red]✗[/] Passphrase is required for local backend")
                return False

            os.environ["PULUMI_CONFIG_PASSPHRASE"] = passphrase

            result = subprocess.run(
                ["pulumi", "login", "--local"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                console.print("  [green]✓[/] Using local backend (~/.pulumi)")
            else:
                console.print(
                    f"  [red]✗[/] Failed to set up local backend: {result.stderr.strip()}"
                )
                return False
        else:
            # check if already logged in to cloud
            result = subprocess.run(
                ["pulumi", "whoami"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                console.print("  [yellow]![/] Not logged in to Pulumi Cloud")
                console.print("  [dim]Run:[/] pulumi login")
                return False
            console.print(f"  [green]✓[/] Using Pulumi Cloud ({result.stdout.strip()})")

        return True

    def _check_pulumi_installed(self) -> bool:
        import shutil

        return shutil.which("pulumi") is not None

    def _print_success(self, output_dir: str):
        console.print()
        console.print(
            Panel.fit(
                "[bold green]Setup Complete![/]",
                border_style="green",
                padding=(0, 2),
            )
        )
        console.print()
        dir_name = os.path.basename(os.path.abspath(output_dir))
        console.print("  [dim]To deploy, run:[/]")
        console.print(f"    [bold {BLUE}]cd {dir_name}[/]")
        console.print(f"    [bold {BLUE}]pulumi up[/]")
        console.print()


# ---------------------------------------------------------------------------
# AWS Setup Wizard
# ---------------------------------------------------------------------------


class AWSPreflightChecker:
    def __init__(self, region: str, azs: list[str], cidr: str):
        import boto3

        self.region = region
        self.azs = azs
        self.cidr = cidr
        self.results: list[PreflightResult] = []

        self.ec2 = boto3.client("ec2", region_name=region)
        self.eks = boto3.client("eks", region_name=region)
        self.servicequotas = boto3.client("service-quotas", region_name=region)

    def run_checks(self) -> bool:
        checks = [
            ("VPC Quota", self._check_vpc_quota),
            ("Elastic IPs", self._check_eip_quota),
            ("NAT Gateways", self._check_nat_gateway_quota),
            ("Internet Gateways", self._check_igw_quota),
            ("EKS Clusters", self._check_eks_cluster_quota),
            ("Network Load Balancers", self._check_nlb_quota),
            ("Availability Zones", self._check_az_availability),
            ("Instance Types", self._check_instance_types),
            ("VPC CIDR", self._check_cidr_conflicts),
        ]

        for name, check_fn in checks:
            with Status(f"  [dim]Checking {name}...[/]", console=console, spinner="dots"):
                check_fn()

            # print the result that was just added
            r = self.results[-1]
            status = "✓" if r.passed else "✗"
            color = "green" if r.passed else "red"
            console.print(f"  [{color}]{status}[/] {r.name}: {r.message}")
            if r.details and not r.passed:
                console.print(f"    [dim]{r.details}[/]")

        failed = [r for r in self.results if not r.passed]
        return len(failed) == 0

    def _add_result(self, name: str, passed: bool, message: str, details: str | None = None):
        result = PreflightResult(name, passed, message, details)
        self.results.append(result)

    def _get_quota(self, service_code: str, quota_code: str) -> float | None:
        try:
            response = self.servicequotas.get_service_quota(
                ServiceCode=service_code, QuotaCode=quota_code
            )
            return response["Quota"]["Value"]
        except Exception:
            try:
                response = self.servicequotas.get_aws_default_service_quota(
                    ServiceCode=service_code, QuotaCode=quota_code
                )
                return response["Quota"]["Value"]
            except Exception:
                return None

    def _check_vpc_quota(self):
        quota = self._get_quota("vpc", "L-F678F1CE") or 5
        try:
            vpcs = self.ec2.describe_vpcs()
            current = len(vpcs["Vpcs"])
            available = int(quota) - current

            self._add_result(
                "VPC Quota",
                available >= 1,
                f"{available} available [dim](using {current}/{int(quota)})[/]",
                "Request a quota increase via AWS Service Quotas" if available < 1 else None,
            )
        except Exception as e:
            self._add_result("VPC Quota", False, "Failed to check", str(e))

    def _check_eip_quota(self):
        needed = len(self.azs)  # one per AZ for NAT gateways
        quota = self._get_quota("ec2", "L-0263D0A3") or 5
        try:
            addresses = self.ec2.describe_addresses()
            current = len(addresses["Addresses"])
            available = int(quota) - current

            self._add_result(
                "Elastic IPs",
                available >= needed,
                f"{available} available, need {needed}",
                "Request quota increase for 'EC2-VPC Elastic IPs'" if available < needed else None,
            )
        except Exception as e:
            self._add_result("Elastic IPs", False, "Failed to check", str(e))

    def _check_nat_gateway_quota(self):
        quota = self._get_quota("vpc", "L-FE5A380F") or 5
        try:
            response = self.ec2.describe_nat_gateways(
                Filters=[{"Name": "state", "Values": ["available", "pending"]}]
            )

            # count NAT gateways per AZ (quota is per-AZ, not per-account)
            nat_gateways_by_az = {}
            for nat_gw in response["NatGateways"]:
                # get subnet AZ for this NAT gateway
                subnet_id = nat_gw.get("SubnetId")
                if subnet_id:
                    subnet_response = self.ec2.describe_subnets(SubnetIds=[subnet_id])
                    if subnet_response["Subnets"]:
                        az = subnet_response["Subnets"][0]["AvailabilityZone"]
                        nat_gateways_by_az[az] = nat_gateways_by_az.get(az, 0) + 1

            # check each requested AZ has capacity
            insufficient_azs = []
            for az in self.azs:
                current_in_az = nat_gateways_by_az.get(az, 0)
                available_in_az = int(quota) - current_in_az
                if available_in_az < 1:
                    insufficient_azs.append(f"{az} ({current_in_az}/{int(quota)})")

            if insufficient_azs:
                self._add_result(
                    "NAT Gateways",
                    False,
                    f"Insufficient capacity in: {', '.join(insufficient_azs)}",
                    "Request quota increase for 'NAT gateways per AZ'",
                )
            else:
                self._add_result(
                    "NAT Gateways",
                    True,
                    f"All AZs have capacity [dim](quota: {int(quota)} per AZ)[/]",
                )
        except Exception as e:
            self._add_result("NAT Gateways", False, "Failed to check", str(e))

    def _check_igw_quota(self):
        quota = self._get_quota("vpc", "L-A4707A72") or 5
        try:
            response = self.ec2.describe_internet_gateways()
            current = len(response["InternetGateways"])
            available = int(quota) - current

            self._add_result(
                "Internet Gateways",
                available >= 1,
                f"{available} available",
                "Request quota increase for 'Internet gateways per Region'"
                if available < 1
                else None,
            )
        except Exception as e:
            self._add_result("Internet Gateways", False, "Failed to check", str(e))

    def _check_nlb_quota(self):
        import boto3

        quota = self._get_quota("elasticloadbalancing", "L-53DA6B97") or 50
        try:
            elb = boto3.client("elbv2", region_name=self.region)
            response = elb.describe_load_balancers()
            nlbs = [lb for lb in response["LoadBalancers"] if lb["Type"] == "network"]
            current = len(nlbs)
            available = int(quota) - current

            self._add_result(
                "Network Load Balancers",
                available >= 1,
                f"{available} available",
                "Request quota increase for 'Network Load Balancers'" if available < 1 else None,
            )
        except Exception as e:
            self._add_result("Network Load Balancers", False, "Failed to check", str(e))

    def _check_eks_cluster_quota(self):
        quota = self._get_quota("eks", "L-1194D53C") or 100
        try:
            clusters = self.eks.list_clusters()
            current = len(clusters["clusters"])
            available = int(quota) - current

            self._add_result(
                "EKS Cluster Quota",
                available >= 1,
                f"{available} available [dim](using {current}/{int(quota)})[/]",
                "Request quota increase for 'Clusters'" if available < 1 else None,
            )
        except Exception as e:
            self._add_result("EKS Cluster Quota", False, "Failed to check", str(e))

    def _check_az_availability(self):
        try:
            azs_response = self.ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )
            available_azs = [az["ZoneName"] for az in azs_response["AvailabilityZones"]]

            missing = [az for az in self.azs if az not in available_azs]
            self._add_result(
                "Availability Zones",
                len(missing) == 0,
                "All requested AZs available"
                if not missing
                else f"AZs not available: {', '.join(missing)}",
                f"Available AZs: {', '.join(available_azs)}" if missing else None,
            )
        except Exception as e:
            self._add_result("Availability Zones", False, "Failed to check", str(e))

    def _check_instance_types(self):
        # check all instance types needed for the cluster
        instance_types = ["m6idn.large", "i7ie.large", "m6idn.xlarge", "r6in.large"]
        all_available = True
        unavailable = []

        try:
            for instance_type in instance_types:
                response = self.ec2.describe_instance_type_offerings(
                    LocationType="availability-zone",
                    Filters=[
                        {"Name": "instance-type", "Values": [instance_type]},
                        {"Name": "location", "Values": self.azs},
                    ],
                )
                offered_azs = [o["Location"] for o in response["InstanceTypeOfferings"]]
                missing = [az for az in self.azs if az not in offered_azs]
                if missing:
                    all_available = False
                    unavailable.append(f"{instance_type}")

            self._add_result(
                "Instance Types",
                all_available,
                "All required types available"
                if all_available
                else f"Unavailable: {', '.join(unavailable)}",
                "Choose different AZs or request capacity" if not all_available else None,
            )
        except Exception as e:
            self._add_result("Instance Types", False, "Failed to check", str(e))

    def _check_cidr_conflicts(self):
        import ipaddress

        try:
            target_net = ipaddress.ip_network(self.cidr)
        except ValueError:
            self._add_result(
                "VPC CIDR",
                False,
                f"Invalid CIDR: {self.cidr}",
                "Enter a valid CIDR block (e.g., 10.0.0.0/16)",
            )
            return

        # must be /16 for subnet calculation
        if target_net.prefixlen != 16:
            self._add_result(
                "VPC CIDR",
                False,
                f"CIDR must be a /16 (got /{target_net.prefixlen})",
                "Subnet calculation requires a /16 network (e.g., 10.0.0.0/16)",
            )
            return

        # must be RFC 1918 private range (AWS rejects CIDRs in 100.64.0.0/10 and other reserved ranges)
        rfc1918_ranges = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ]
        if not any(target_net.subnet_of(r) for r in rfc1918_ranges):
            self._add_result(
                "VPC CIDR",
                False,
                f"{self.cidr} is not in an RFC 1918 private range",
                "Use a /16 block like 10.0.0.0/16, 172.16.0.0/16, or 192.168.0.0/16. "
                "See https://docs.aws.amazon.com/vpc/latest/userguide/vpc-cidr-blocks.html",
            )
            return

        # check overlap with existing VPCs
        try:
            response = self.ec2.describe_vpcs()
            conflicts = []
            for vpc in response["Vpcs"]:
                vpc_cidr = vpc.get("CidrBlock", "")
                if not vpc_cidr:
                    continue
                try:
                    existing_net = ipaddress.ip_network(vpc_cidr)
                    if target_net.overlaps(existing_net):
                        conflicts.append(vpc_cidr)
                except ValueError:
                    continue

            self._add_result(
                "VPC CIDR",
                len(conflicts) == 0,
                f"{self.cidr} available"
                if not conflicts
                else f"Conflicts with: {', '.join(conflicts)}",
                "Choose a different CIDR range to avoid conflicts" if conflicts else None,
            )
        except Exception as e:
            self._add_result("VPC CIDR", False, "Failed to check", str(e))


class AWSSetupWizard(BaseSetupWizard):
    TOTAL_STEPS = 15
    HEADER_TITLE = "Pinecone BYOC Setup Wizard"
    HEADER_SUBTITLE = "This wizard will set up everything you need to deploy Pinecone BYOC."
    DEFAULT_CIDR = "10.0.0.0/16"
    CIDR_DESC = "The IP range for your VPC (/16 from an RFC 1918 private range, must not conflict with existing VPCs)"
    DELETION_PROTECTION_DESC = "Protect RDS databases and S3 buckets from accidental deletion"
    PRIVATE_ACCESS_DESC = "Private access requires AWS PrivateLink (more secure)"
    METADATA_NAME = "tags"
    CLOUD_NAME = "AWS"

    def run(self, output_dir: str = ".") -> bool:
        if self._headless:
            return self._run_headless(output_dir)

        self._print_header()

        api_key = self._get_api_key()
        if not api_key:
            return False

        if not self._validate_api_key(api_key):
            return False

        if not self._validate_aws_creds():
            return False

        region = self._get_region()
        azs = self._get_azs(region)
        custom_ami_id = self._get_custom_ami_id()
        kms_key_arn = self._get_kms_key_arn()
        cidr = self._get_cidr()
        deletion_protection = self._get_deletion_protection()
        public_access = self._get_public_access()
        tags = self._get_custom_metadata()

        if not self._run_preflight_checks(region, azs, cidr):
            return False

        project_name = self._get_project_name()

        if not self._setup_pulumi_backend():
            return False

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            region,
            azs,
            cidr,
            deletion_protection,
            public_access,
            tags,
            custom_ami_id=custom_ami_id,
            kms_key_arn=kms_key_arn,
        )

    def _run_headless(self, output_dir: str) -> bool:
        console.print("  [dim]Running in headless mode (reading from environment)[/]")

        api_key = os.environ.get("PINECONE_API_KEY")
        if not api_key:
            console.print("  [red]✗[/] PINECONE_API_KEY environment variable is required")
            return False

        region = os.environ.get("PINECONE_REGION", "us-east-1")
        azs_str = os.environ.get("PINECONE_AZS", f"{region}a,{region}b")
        azs = [az.strip() for az in azs_str.split(",") if az.strip()]
        cidr = os.environ.get("PINECONE_VPC_CIDR", self.DEFAULT_CIDR)
        deletion_protection = (
            os.environ.get("PINECONE_DELETION_PROTECTION", "true").lower() == "true"
        )
        public_access = os.environ.get("PINECONE_PUBLIC_ACCESS", "true").lower() == "true"
        project_name = os.environ.get("PINECONE_PROJECT_NAME", "pinecone-byoc")
        custom_ami_id = os.environ.get("PINECONE_CUSTOM_AMI_ID", "") or None
        kms_key_arn = os.environ.get("PINECONE_KMS_KEY_ARN", "") or None

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            region,
            azs,
            cidr,
            deletion_protection,
            public_access,
            {},
            custom_ami_id=custom_ami_id,
            kms_key_arn=kms_key_arn,
        )

    def _validate_aws_creds(self) -> bool:
        console.print()
        console.print(f"  {self._step('AWS Credentials')}")
        console.print()

        with Status("  [dim]Validating AWS credentials...[/]", console=console, spinner="dots"):
            try:
                import boto3

                sts = boto3.client("sts")
                identity = sts.get_caller_identity()
                account_id = identity["Account"]
            except Exception as e:
                console.print(f"  [red]✗[/] AWS credentials invalid: {e}")
                console.print()
                console.print("  [dim]Make sure you have valid AWS credentials configured.[/]")
                console.print("  [dim]You can set them via:[/]")
                console.print("    [dim]· AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY[/]")
                console.print("    [dim]· aws configure[/]")
                console.print("    [dim]· AWS_PROFILE environment variable[/]")
                return False

        console.print(f"  [green]✓[/] AWS credentials valid [dim](Account: {account_id})[/]")
        return True

    def _get_region(self) -> str:
        console.print()
        console.print(f"  {self._step('AWS Region')}")
        console.print()
        return self._prompt("Enter AWS region", "us-east-1")

    def _fetch_azs(self, region: str) -> list[str]:
        import boto3

        try:
            ec2 = boto3.client("ec2", region_name=region)
            response = ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )
            return sorted([az["ZoneName"] for az in response["AvailabilityZones"]])
        except Exception as e:
            console.print(f"  [yellow]⚠[/] Could not fetch AZs from AWS: {e}")
            return [f"{region}a", f"{region}b", f"{region}c"]

    def _get_azs(self, region: str) -> list[str]:
        console.print()
        console.print(f"  {self._step('Availability Zones')}")
        console.print()

        with Status("  [dim]Fetching availability zones...[/]", console=console, spinner="dots"):
            available = self._fetch_azs(region)

        console.print(f"  [dim]Available in {region}:[/] {', '.join(available)}")
        default_azs = available[:2]

        azs_input = self._prompt("Enter AZs (comma-separated)", ",".join(default_azs))
        azs = [az.strip() for az in azs_input.split(",")]
        return azs

    def _get_custom_ami_id(self) -> str | None:
        console.print()
        console.print(f"  {self._step('Custom AMI (Optional)')}")
        console.print(
            "  [dim]Specify a custom AMI ID for EKS nodes (leave blank for default AWS AMI)[/]"
        )
        console.print()
        ami_id = self._prompt("Enter AMI ID (or press Enter to skip)", "")
        return ami_id or None

    def _get_kms_key_arn(self) -> str | None:
        console.print()
        console.print(f"  {self._step('KMS Key (Optional)')}")
        console.print(
            "  [dim]Provide a KMS key ARN to encrypt S3 buckets and RDS with your own key.[/]"
        )
        console.print(
            "  [dim]Leave blank to use default AWS-managed encryption (AES256/default RDS key).[/]"
        )
        console.print()
        arn = self._prompt("Enter KMS key ARN (or press Enter to skip)", "")
        return arn or None

    def _run_preflight_checks(self, region: str, azs: list[str], cidr: str) -> bool:
        console.print()
        console.print(f"  {self._step('Preflight Checks')}")
        console.print()

        checker = AWSPreflightChecker(region, azs, cidr)
        if not checker.run_checks():
            console.print()
            console.print(
                "  [red]Preflight checks failed. Fix the issues above before proceeding.[/]"
            )
            return False

        return True

    def _generate_project(
        self,
        output_dir: str,
        project_name: str,
        api_key: str,
        region: str,
        azs: list[str],
        cidr: str,
        deletion_protection: bool,
        public_access: bool,
        tags: dict[str, str],
        custom_ami_id: str | None = None,
        kms_key_arn: str | None = None,
    ):
        console.print()

        console.print(f"  {self._step('Creating Project')}")
        console.print()

        if not self._check_pulumi_installed():
            console.print("  [red]✗[/] Pulumi CLI not found")
            console.print("  [dim]Install Pulumi first:[/] https://www.pulumi.com/docs/install/")
            return False

        pulumi_yaml = {
            "name": project_name,
            "runtime": {
                "name": "python",
                "options": {"virtualenv": ".venv", "toolchain": "uv"},
            },
            "description": "Pinecone BYOC deployment",
        }

        os.makedirs(output_dir, exist_ok=True)
        pulumi_yaml_path = os.path.join(output_dir, "Pulumi.yaml")
        with open(pulumi_yaml_path, "w") as f:
            yaml.dump(pulumi_yaml, f, default_flow_style=False)
        console.print("  [green]✓[/] Created Pulumi.yaml")

        # create __main__.py
        main_py = '''"""Pinecone BYOC deployment (AWS)."""

import pulumi
from pulumi_pinecone_byoc.aws import PineconeAWSCluster, PineconeAWSClusterArgs

config = pulumi.Config()

cluster = PineconeAWSCluster(
    name="pinecone-aws-cluster",
    args=PineconeAWSClusterArgs(
        pinecone_api_key=config.require_secret("pinecone-api-key"),
        pinecone_version=config.require("pinecone-version"),
        region=config.require("region"),
        vpc_cidr=config.get("vpc-cidr"),
        availability_zones=config.require_object("availability-zones"),
        deletion_protection=config.get_bool("deletion-protection") if config.get_bool("deletion-protection") is not None else True,
        public_access_enabled=config.get_bool("public-access-enabled") if config.get_bool("public-access-enabled") is not None else True,
        custom_ami_id=config.get("custom-ami-id"),
        kms_key_arn=config.get("kms-key-arn"),
        tags=config.get_object("tags"),
    ),
)

update_kubeconfig_command = cluster.name.apply(
    lambda name: f"aws eks update-kubeconfig --region {config.require('region')} --name {name}"
)
pulumi.export("environment", cluster.environment_name)
pulumi.export("update_kubeconfig_command", update_kubeconfig_command)
if config.get_bool("public-access-enabled") is False:
    pulumi.export("vpc_endpoint_service_name", cluster.vpc_endpoint_service_name)
'''

        main_py_path = os.path.join(output_dir, "__main__.py")
        with open(main_py_path, "w") as f:
            f.write(main_py)
        console.print("  [green]✓[/] Created __main__.py")

        # create pyproject.toml for uv toolchain to install dependencies
        pyproject_content = """[project]
name = "pinecone-byoc"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pulumi-pinecone-nexus-byoc[aws]"]
"""
        if self._local_package_path:
            pyproject_content += (
                "\n[tool.uv.sources]\n"
                f'pulumi-pinecone-nexus-byoc = {{ path = "{self._local_package_path}", editable = true }}\n'
            )
        pyproject_path = os.path.join(output_dir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(pyproject_content)
        console.print("  [green]✓[/] Created pyproject.toml")

        # create stack config
        stack_name = self._stack_name
        deletion_protection_str = str(deletion_protection).lower()
        public_access_str = str(public_access).lower()
        config_content = f"""config:
  aws:region: {region}
  {project_name}:region: {region}
  {project_name}:pinecone-version: {PINECONE_VERSION}
  {project_name}:vpc-cidr: {cidr}
  {project_name}:deletion-protection: {deletion_protection_str}
  {project_name}:public-access-enabled: {public_access_str}
  {project_name}:availability-zones:
"""
        for az in azs:
            config_content += f"    - {az}\n"

        # add custom AMI ID if provided
        if custom_ami_id:
            config_content += f"  {project_name}:custom-ami-id: {custom_ami_id}\n"

        # add customer KMS key ARN if provided
        if kms_key_arn:
            config_content += f"  {project_name}:kms-key-arn: {kms_key_arn}\n"

        # add tags if provided (quote values to handle YAML special chars)
        if tags:
            config_content += f"  {project_name}:tags:\n"
            for key, value in tags.items():
                config_content += f'    {key}: "{value}"\n'

        config_path = os.path.join(output_dir, f"Pulumi.{stack_name}.yaml")
        with open(config_path, "w") as f:
            f.write(config_content)
        console.print(f"  [green]✓[/] Created Pulumi.{stack_name}.yaml")

        if self._skip_install:
            return True

        # install dependencies with uv
        with Status("  [dim]Installing dependencies...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                ["uv", "sync"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            # get installed version
            version_result = subprocess.run(
                ["uv", "pip", "show", "pulumi-pinecone-nexus-byoc"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )
            pkg_version = "unknown"
            for line in version_result.stdout.splitlines():
                if line.startswith("Version:"):
                    pkg_version = line.split(":", 1)[1].strip()
                    break
            console.print(
                f"  [green]✓[/] Dependencies installed [dim](pulumi-pinecone-nexus-byoc v{pkg_version})[/]"
            )
        else:
            console.print(f"  [red]✗[/] Failed to install dependencies: {result.stderr.strip()}")
            console.print("  [dim]Run manually:[/] uv sync")
            return False

        # init stack
        with Status("  [dim]Initializing stack...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "stack",
                    "select",
                    "--create",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            console.print(f"  [green]✓[/] Stack {stack_name} ready")
        else:
            console.print(f"  [yellow]⚠[/] Stack init: {result.stderr.strip()}")

        # set api key as secret
        with Status("  [dim]Storing API key securely...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "config",
                    "set",
                    "--secret",
                    "pinecone-api-key",
                    api_key,
                    "--stack",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            console.print(f"  [red]✗[/] Failed to store API key: {result.stderr.strip()}")
            console.print(
                "  [dim]Run manually:[/] pulumi config set --secret pinecone-api-key <key>"
            )
            return False

        console.print("  [green]✓[/] API key stored securely")

        self._print_success(output_dir)
        return True


# ---------------------------------------------------------------------------
# GCP Setup Wizard
# ---------------------------------------------------------------------------


class GCPPreflightChecker:
    def __init__(self, project_id: str, region: str, zones: list[str], cidr: str):
        self.project_id = project_id
        self.region = region
        self.zones = zones
        self.cidr = cidr
        self.results: list[PreflightResult] = []
        self._missing_apis: list[str] = []

    def run_checks(self) -> bool:
        checks = [
            ("GCP APIs", self._check_apis_enabled),
            ("VPC Networks", self._check_vpc_quota),
            ("External IPs", self._check_external_ip_quota),
            ("GKE Clusters", self._check_gke_quota),
            ("Machine Types", self._check_machine_types),
            ("Availability Zones", self._check_zones),
            ("VPC CIDR", self._check_cidr_conflicts),
        ]

        for name, check_fn in checks:
            with Status(f"  [dim]Checking {name}...[/]", console=console, spinner="dots"):
                check_fn()

            # Offer to enable missing APIs -- done outside the Status spinner so
            # the prompt/raw-terminal input isn't garbled by the live display.
            if name == "GCP APIs" and self._missing_apis:
                missing = self._missing_apis
                answer = _read_input_with_placeholder(
                    f"{len(missing)} required GCP APIs are not enabled. Enable them now? (Y/n)",
                    "Y",
                )
                if answer.lower() in ("y", "yes", ""):
                    with Status("  [dim]Enabling APIs...[/]", console=console, spinner="dots"):
                        enable = subprocess.run(
                            [
                                "gcloud",
                                "services",
                                "enable",
                                *missing,
                                f"--project={self.project_id}",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                    if enable.returncode == 0:
                        self.results[-1] = PreflightResult(
                            "GCP APIs",
                            True,
                            f"Enabled {len(missing)} previously-missing APIs",
                        )
                    else:
                        self.results[-1] = PreflightResult(
                            "GCP APIs",
                            False,
                            f"Failed to enable: {enable.stderr.strip().split(chr(10))[0]}",
                            f"Run: gcloud services enable {' '.join(missing)} "
                            f"--project={self.project_id}",
                        )

            # print the result that was just added
            r = self.results[-1]
            status = "✓" if r.passed else "✗"
            color = "green" if r.passed else "red"
            console.print(f"  [{color}]{status}[/] {r.name}: {r.message}")
            if r.details and not r.passed:
                console.print(f"    [dim]{r.details}[/]")

        failed = [r for r in self.results if not r.passed]
        return len(failed) == 0

    def _add_result(self, name: str, passed: bool, message: str, details: str | None = None):
        result = PreflightResult(name, passed, message, details)
        self.results.append(result)

    def _gcloud_json(self, args: list[str]):
        import json

        result = subprocess.run(
            ["gcloud"] + args + [f"--project={self.project_id}", "--format=json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip().split("\n")[0])

        return json.loads(result.stdout)

    def _check_apis_enabled(self):
        required_apis = [
            "alloydb.googleapis.com",
            "autoscaling.googleapis.com",
            "cloudapis.googleapis.com",
            "cloudkms.googleapis.com",
            "cloudresourcemanager.googleapis.com",
            "compute.googleapis.com",
            "container.googleapis.com",
            "dns.googleapis.com",
            "domains.googleapis.com",
            "iam.googleapis.com",
            "iamcredentials.googleapis.com",
            "networkmanagement.googleapis.com",
            "secretmanager.googleapis.com",
            "servicedirectory.googleapis.com",
            "servicemanagement.googleapis.com",
            "servicenetworking.googleapis.com",
            "siteverification.googleapis.com",
            "storage.googleapis.com",
        ]

        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "services",
                    "list",
                    "--enabled",
                    "--format=value(config.name)",
                    f"--project={self.project_id}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self._add_result(
                    "GCP APIs",
                    False,
                    f"Failed: {result.stderr.strip().split(chr(10))[0]}",
                )
                return

            enabled_apis = result.stdout.strip().split("\n")
            missing = [api for api in required_apis if api not in enabled_apis]

            if missing:
                self._missing_apis = missing
                short_names = [api.replace(".googleapis.com", "") for api in missing]
                self._add_result(
                    "GCP APIs",
                    False,
                    f"{len(missing)} missing: {', '.join(short_names)}",
                    f"Run: gcloud services enable {' '.join(missing)} --project={self.project_id}",
                )
            else:
                self._missing_apis = []
                self._add_result(
                    "GCP APIs", True, f"All {len(required_apis)} required APIs enabled"
                )
        except Exception as e:
            self._add_result("GCP APIs", False, f"Failed to check: {e}")

    def _check_vpc_quota(self):
        try:
            networks = self._gcloud_json(["compute", "networks", "list"])
            current = len(networks) if isinstance(networks, list) else 0
            quota = 15
            available = quota - current
            self._add_result(
                "VPC Networks",
                available >= 1,
                f"{available} available [dim](using {current}/{quota})[/]",
                "Request quota increase for 'VPC networks'" if available < 1 else None,
            )
        except Exception as e:
            self._add_result("VPC Networks", False, f"Failed to check: {e}")

    def _check_external_ip_quota(self):
        needed = 1  # one for external ingress
        try:
            addresses = self._gcloud_json(
                ["compute", "addresses", "list", f"--regions={self.region}"]
            )
            current = len(addresses) if isinstance(addresses, list) else 0
            quota = 8  # default regional static IP quota
            available = quota - current
            self._add_result(
                "External IPs",
                available >= needed,
                f"{available} available, need {needed} [dim](using {current}/{quota})[/]",
                "Request quota increase for 'Static IP addresses'" if available < needed else None,
            )
        except Exception as e:
            self._add_result("External IPs", False, f"Failed to check: {e}")

    def _check_gke_quota(self):
        try:
            data = self._gcloud_json(["container", "clusters", "list"])
            current = len(data) if isinstance(data, list) else 0
            quota = 50
            available = quota - current
            self._add_result(
                "GKE Clusters",
                available >= 1,
                f"{available} available [dim](using {current}/{quota})[/]",
            )
        except Exception as e:
            self._add_result("GKE Clusters", False, f"Failed to check: {e}")

    def _check_machine_types(self):
        machine_types = ["n2-standard-4", "n2-standard-2", "n2-highmem-2"]
        unavailable = []

        try:
            for zone in self.zones:
                result = subprocess.run(
                    [
                        "gcloud",
                        "compute",
                        "machine-types",
                        "list",
                        f"--project={self.project_id}",
                        f"--zones={zone}",
                        "--format=value(name)",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    self._add_result(
                        "Machine Types",
                        False,
                        f"Failed: {result.stderr.strip().split(chr(10))[0]}",
                    )
                    return

                available = result.stdout.strip().split("\n")
                for mt in machine_types:
                    if mt not in available:
                        unavailable.append(f"{mt} in {zone}")

            self._add_result(
                "Machine Types",
                len(unavailable) == 0,
                "All required types available"
                if not unavailable
                else f"Unavailable: {', '.join(unavailable)}",
                "Choose different zones or machine types" if unavailable else None,
            )
        except Exception as e:
            self._add_result("Machine Types", False, f"Failed to check: {e}")

    def _check_zones(self):
        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "compute",
                    "zones",
                    "list",
                    "--format=value(name)",
                    f"--project={self.project_id}",
                    f"--filter=region:{self.region}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self._add_result(
                    "Availability Zones",
                    False,
                    f"Failed: {result.stderr.strip().split(chr(10))[0]}",
                )
                return

            available_zones = [z for z in result.stdout.strip().split("\n") if z]
            invalid = [zone for zone in self.zones if zone not in available_zones]

            if invalid:
                self._add_result(
                    "Availability Zones",
                    False,
                    f"Invalid zones: {', '.join(invalid)}",
                    f"Valid zones for {self.region}: {', '.join(available_zones)}",
                )
            else:
                self._add_result("Availability Zones", True, "All requested zones available")
        except Exception as e:
            self._add_result("Availability Zones", False, f"Failed to check: {e}")

    def _check_cidr_conflicts(self):
        import ipaddress

        try:
            target_net = ipaddress.ip_network(self.cidr)
        except ValueError:
            self._add_result(
                "VPC CIDR",
                False,
                f"Invalid CIDR: {self.cidr}",
                "Enter a valid CIDR block (e.g., 10.112.0.0/12)",
            )
            return

        try:
            networks = self._gcloud_json(["compute", "networks", "list"])
            if not isinstance(networks, list):
                networks = []

            conflicts = []
            # check subnets directly in the region
            result = subprocess.run(
                [
                    "gcloud",
                    "compute",
                    "networks",
                    "subnets",
                    "list",
                    f"--project={self.project_id}",
                    f"--regions={self.region}",
                    "--format=value(ipCidrRange,network)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split()
                    if parts:
                        try:
                            existing_net = ipaddress.ip_network(parts[0])
                            if target_net.overlaps(existing_net):
                                conflicts.append(parts[0])
                        except ValueError:
                            continue

            if conflicts:
                self._add_result(
                    "VPC CIDR",
                    False,
                    f"{self.cidr} conflicts with existing subnets: {', '.join(conflicts)}",
                    "Choose a non-overlapping CIDR block",
                )
            else:
                self._add_result("VPC CIDR", True, f"{self.cidr} has no conflicts")
        except Exception as e:
            self._add_result("VPC CIDR", False, f"Failed to check: {e}")


class GCPSetupWizard(BaseSetupWizard):
    HEADER_TITLE = "Pinecone BYOC Setup Wizard - GCP"
    HEADER_SUBTITLE = "This wizard will set up everything you need to deploy Pinecone BYOC on GCP."
    # one more than the base flow: GCP adds a Nexus enablement step (task 2.7).
    TOTAL_STEPS = 14
    DEFAULT_CIDR = "10.112.0.0/12"
    DELETION_PROTECTION_DESC = "Protect AlloyDB databases and GCS buckets from accidental deletion"
    PRIVATE_ACCESS_DESC = "Private access requires Private Service Connect (more secure)"
    METADATA_NAME = "labels"
    CLOUD_NAME = "GCP"

    def run(self, output_dir: str = ".") -> bool:
        if self._headless:
            return self._run_headless(output_dir)

        self._print_header()

        # Fail-fast: input-independent auth/tooling checks run BEFORE the long
        # interactive flow, so stale/missing auth fails in seconds instead of
        # after the operator fills in the whole wizard.
        if not self._run_early_auth_checks():
            return False

        api_key = self._get_api_key()
        if not api_key:
            return False

        if not self._validate_api_key(api_key):
            return False

        project_id = self._validate_gcp_creds()
        if not project_id:
            return False

        project_id = self._get_project_id(project_id)
        region = self._get_region()
        zones = self._get_zones(project_id, region)
        cidr = self._get_cidr()
        deletion_protection = self._get_deletion_protection()
        public_access = self._get_public_access()
        labels = self._get_custom_metadata()
        nexus = self._get_nexus_config()

        if not self._run_preflight_checks(project_id, region, zones, cidr):
            return False

        project_name = self._get_project_name()

        if not self._setup_pulumi_backend():
            return False

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            project_id,
            region,
            zones,
            cidr,
            deletion_protection,
            public_access,
            labels,
            nexus,
        )

    def _run_headless(self, output_dir: str) -> bool:
        console.print("  [dim]Running in headless mode (reading from environment)[/]")

        api_key = os.environ.get("PINECONE_API_KEY")
        if not api_key:
            console.print("  [red]✗[/] PINECONE_API_KEY environment variable is required")
            return False

        project_id = os.environ.get("GCP_PROJECT")
        if not project_id:
            console.print("  [red]✗[/] GCP_PROJECT environment variable is required")
            return False

        region = os.environ.get("PINECONE_REGION", "us-central1")
        zones_str = os.environ.get("PINECONE_AZS", f"{region}-a,{region}-b")
        zones = [z.strip() for z in zones_str.split(",") if z.strip()]
        cidr = os.environ.get("PINECONE_VPC_CIDR", self.DEFAULT_CIDR)
        deletion_protection = (
            os.environ.get("PINECONE_DELETION_PROTECTION", "true").lower() == "true"
        )
        public_access = os.environ.get("PINECONE_PUBLIC_ACCESS", "true").lower() == "true"
        project_name = os.environ.get("PINECONE_PROJECT_NAME", "pinecone-byoc")

        # Nexus is opt-in; default off so headless DB-only installs are unchanged.
        if os.environ.get("PINECONE_NEXUS_ENABLED", "false").lower() == "true":
            # The Pinecone gCPS project UUID the BYOC vault belongs to (matched
            # against projects.id by CPGW). Required, and must NOT be the GCP
            # project name -- pass it as its own var, distinct from GCP_PROJECT.
            byoc_project_id = os.environ.get("PINECONE_BYOC_PROJECT_ID", "").strip()
            if not byoc_project_id:
                console.print(
                    "  [red]✗[/] PINECONE_BYOC_PROJECT_ID environment variable is required"
                    " when Nexus is enabled (the Pinecone gCPS project UUID)"
                )
                return False
            if not _is_uuid(byoc_project_id):
                console.print(
                    "  [red]✗[/] PINECONE_BYOC_PROJECT_ID must be a Pinecone gCPS project"
                    " UUID (e.g. 123e4567-e89b-12d3-a456-426614174000), not the GCP"
                    " project name"
                )
                return False
            # The GCS bucket-name prefix Nexus storage is provisioned under.
            # OPTIONAL override on GCP: when unset, the package derives it from
            # the cell name (`pc-nexus-{cell}`), which is minted server-side
            # mid-deploy and so can't be supplied in advance. Accepts the
            # canonical PINECONE_NEXUS_STORAGE_BUCKET_PREFIX, falling back to the
            # shorter PINECONE_STORAGE_BUCKET_PREFIX alias.
            storage_bucket_prefix = (
                os.environ.get("PINECONE_NEXUS_STORAGE_BUCKET_PREFIX")
                or os.environ.get("PINECONE_STORAGE_BUCKET_PREFIX")
                or ""
            ).strip()
            if storage_bucket_prefix and not _is_storage_bucket_prefix(storage_bucket_prefix):
                console.print(
                    "  [red]✗[/] PINECONE_NEXUS_STORAGE_BUCKET_PREFIX must be a valid"
                    " GCS bucket name prefix: lowercase letters, digits and hyphens,"
                    " starting and ending alphanumeric, and at most"
                    f" {_STORAGE_PREFIX_MAX_LEN} chars (so {{prefix}}-knowledge stays"
                    " <= 63)"
                )
                return False
            nexus: NexusWizardConfig = {
                "enabled": True,
                "byoc_env": os.environ.get("PINECONE_BYOC_ENV", ""),
                "nexus_version": os.environ.get("PINECONE_NEXUS_VERSION", NEXUS_VERSION),
                "image_registry": os.environ.get(
                    "PINECONE_NEXUS_IMAGE_REGISTRY", NEXUS_IMAGE_REGISTRY
                ),
                "inference_base": os.environ.get(
                    "PINECONE_INFERENCE_BASE", "https://api.pinecone.io"
                ),
                "byoc_project_id": byoc_project_id,
                # Optional override; blank => package derives `pc-nexus-{cell}`.
                "storage_bucket_prefix": storage_bucket_prefix,
                # Inference models from env JSON, or None -> default template.
                "inference_models_toml": self._headless_inference_models_toml(),
            }
        else:
            nexus = NexusWizardConfig(enabled=False)

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            project_id,
            region,
            zones,
            cidr,
            deletion_protection,
            public_access,
            {},
            nexus,
        )

    def _run_early_auth_checks(self) -> bool:
        """Input-independent fail-fast checks (host tools, live GCP ADC, Pulumi
        backend/SSO session).

        These need no operator input, so they run up front: a customer with a
        missing tool or a stale auth session fails here, not after filling in
        the whole wizard. The impersonation/RAPT check is OFF by default and
        only runs when ADC is already impersonation-based.
        """
        console.print()
        console.print(f"  {self._step('Auth & tooling preflight')}")

        tools_ok = preflight_checks.check_host_tools()
        auth_ok = preflight_checks.check_auth()

        # RAPT is meaningful only for impersonation-based ADC; never run by
        # default for plain user/SA-key auth.
        if preflight_checks.adc_is_impersonated():
            preflight_checks.check_gcp_rapt()

        # No stack dir yet (the project isn't generated); this warns rather than
        # fails, but still surfaces an obviously-dead backend session early.
        preflight_checks.check_pulumi_session(None)

        if not (tools_ok and auth_ok):
            console.print()
            console.print(
                "  [red]Auth/tooling checks failed. Fix the issues above before proceeding.[/]"
            )
            return False
        return True

    def _validate_gcp_creds(self) -> str | None:
        console.print()
        console.print(f"  {self._step('GCP Credentials')}")
        console.print()

        project_id = None
        with Status("  [dim]Validating GCP credentials...[/]", console=console, spinner="dots"):
            try:
                try:
                    from google.auth import default

                    credentials, project_id = default()
                    if credentials and project_id:
                        console.print(
                            f"  [green]✓[/] GCP credentials valid [dim](Project: {project_id})[/]"
                        )
                except ImportError:
                    pass

                if not project_id:
                    result = subprocess.run(
                        ["gcloud", "config", "get-value", "project"],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        project_id = result.stdout.strip()
                        console.print(
                            f"  [green]✓[/] GCP credentials valid [dim](Project: {project_id})[/]"
                        )
                    else:
                        raise Exception("Could not determine GCP project")

            except Exception as e:
                console.print(f"  [red]✗[/] GCP credentials invalid: {e}")
                console.print()
                console.print("  [dim]Make sure you have valid GCP credentials configured.[/]")
                console.print("  [dim]You can set them via:[/]")
                console.print("    [dim]· gcloud auth application-default login[/]")
                console.print("    [dim]· GOOGLE_APPLICATION_CREDENTIALS environment variable[/]")
                console.print("    [dim]· gcloud config set project PROJECT_ID[/]")
                return None

        # check for gke-gcloud-auth-plugin (required for kubectl/Pulumi to auth to GKE)
        try:
            plugin_check = subprocess.run(
                ["gke-gcloud-auth-plugin", "--version"],
                capture_output=True,
                text=True,
            )
            if plugin_check.returncode != 0:
                raise FileNotFoundError
        except FileNotFoundError:
            console.print("  [red]✗[/] gke-gcloud-auth-plugin not found")
            console.print("  [dim]Install it:[/] gcloud components install gke-gcloud-auth-plugin")
            return None
        console.print("  [green]✓[/] gke-gcloud-auth-plugin installed")

        return project_id

    def _get_project_id(self, detected_project: str) -> str:
        console.print()
        console.print(f"  {self._step('GCP Project ID')}")
        console.print()
        return self._prompt("Enter GCP project ID", detected_project)

    def _get_region(self) -> str:
        console.print()
        console.print(f"  {self._step('GCP Region')}")
        console.print()
        return self._prompt("Enter GCP region", "us-central1")

    def _fetch_zones(self, project_id: str, region: str) -> list[str]:
        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "compute",
                    "zones",
                    "list",
                    "--format=value(name)",
                    f"--project={project_id}",
                    f"--filter=region:{region} AND status:UP",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                zones = sorted([z for z in result.stdout.strip().split("\n") if z])
                if zones:
                    return zones
        except Exception as e:
            console.print(f"  [yellow]⚠[/] Could not fetch zones from GCP: {e}")
        return [f"{region}-a", f"{region}-b", f"{region}-c"]

    def _get_zones(self, project_id: str, region: str) -> list[str]:
        console.print()
        console.print(f"  {self._step('GCP Zones')}")
        console.print()

        with Status("  [dim]Fetching availability zones...[/]", console=console, spinner="dots"):
            available = self._fetch_zones(project_id, region)

        console.print(f"  [dim]Available in {region}:[/] {', '.join(available)}")
        default_zones = available[:2]

        zones_input = self._prompt("Enter zones (comma-separated)", ",".join(default_zones))
        zones = [zone.strip() for zone in zones_input.split(",")]
        return zones

    def _get_nexus_config(self) -> NexusWizardConfig:
        """Prompt for Nexus enablement and inference config (proposal §4.6/§4.7,
        task 2.7). Default is a DB-only install (nexus_enabled=False) so the
        generated project is byte-for-byte unchanged unless Nexus is requested.

        For a "Nexus BYOC" install the wizard collects the BYOC env id
        (PINECONE_BYOC_ENV), the Nexus image tag (nexus-version), and the
        inference base (INFERENCE_BASE). The inference key is not prompted: it
        defaults to the minted deployment key per §10.
        """
        console.print()
        console.print(f"  {self._step('Nexus')}")
        console.print()
        console.print("  [dim]Deploy Nexus alongside the Pinecone DB stack in the same cluster.[/]")

        response = self._prompt("Enable Nexus? (Y/n)", "Y")
        if response.strip().lower() in ("n", "no"):
            return {"enabled": False}

        console.print()
        console.print(
            "  [dim]The `.byoc` deployment environment id Nexus targets for index CRUD.[/]"
        )
        byoc_env = self._prompt(
            "Enter PINECONE_BYOC_ENV (or press Enter to use the minted env)", ""
        )

        console.print()
        console.print("  [dim]The Pinecone gCPS project UUID that the BYOC vault belongs to[/]")
        console.print(
            "  [dim]This is NOT the GCP project name -- it is the gCPS project id"
            " (matched against projects.id), e.g. 123e4567-e89b-12d3-a456-426614174000.[/]"
        )
        while True:
            byoc_project_id = self._prompt("Enter Pinecone gCPS project UUID").strip()
            if _is_uuid(byoc_project_id):
                break
            console.print(
                "  [red]Enter a valid UUID (e.g. 123e4567-e89b-12d3-a456-426614174000);"
                " this is the Pinecone gCPS project id, not the GCP project name.[/]"
            )

        console.print()
        console.print("  [dim]Optional override for the Nexus storage bucket prefix. Leave[/]")
        console.print("  [dim]blank to auto-derive it from the cell name (pc-nexus-<cell>);[/]")
        console.print("  [dim]the cluster provisions {prefix}-source/-knowledge/-archive.[/]")
        while True:
            storage_bucket_prefix = self._prompt(
                "Enter a Nexus storage bucket prefix override (blank = auto-derive)"
            ).strip()
            if not storage_bucket_prefix or _is_storage_bucket_prefix(storage_bucket_prefix):
                break
            console.print(
                "  [red]Enter a valid GCS bucket name prefix: lowercase letters,"
                " digits and hyphens, starting and ending alphanumeric, and at most"
                f" {_STORAGE_PREFIX_MAX_LEN} chars (so {{prefix}}-knowledge stays <="
                " 63).[/]"
            )

        nexus_version = self._prompt("Enter nexus-version", NEXUS_VERSION)

        console.print()
        console.print(
            "  [dim]Container registry for the Nexus images (the `nexus` repo, co-located on the DB registry host).[/]"
        )
        image_registry = self._prompt("Enter nexus image registry", NEXUS_IMAGE_REGISTRY)

        console.print()
        console.print(
            "  [dim]Managed embed/rerank endpoint (the inference key defaults to the deployment key).[/]"
        )
        inference_base = self._prompt("Enter inference base", "https://api.pinecone.io")

        # Guided model catalog + tier selection. None => default template is
        # written and the operator can edit it before `pulumi up`.
        inference_models_toml = self._collect_inference_models()

        console.print()
        if inference_models_toml is None:
            console.print(
                f"  [dim]Using the default Gemini + Pinecone models. Edit [/]"
                f"{NEXUS_INFERENCE_MODELS_FILENAME}[dim] in the generated project to change them.[/]"
            )

        # Provider-key secrets. The default catalog (and the common custom case)
        # uses a single `gemini-api-key` ref; collect it here so the wizard sets
        # `nexus-gemini-api-key` and `nexus-provider-keys.gemini-api-key` itself.
        gemini_api_key = self._get_gemini_api_key()

        # Multi-provider edge case: a customized catalog may reference api_key_refs
        # other than `gemini-api-key`. Prompt (hidden) for each distinct extra ref
        # so its `nexus-provider-keys.<ref>` secret is set too; if the operator
        # skips one, keep printing the manual instruction for that ref.
        provider_keys: dict[str, str] = {}
        extra_refs = sorted(
            ref for ref in _api_key_refs_from_toml(inference_models_toml) if ref != "gemini-api-key"
        )
        for ref in extra_refs:
            console.print()
            console.print(f"  [dim]Provider key for the '{ref}' api_key_ref.[/]")
            key = self._prompt(f"Enter the {ref} provider key", password=True).strip()
            if key:
                provider_keys[ref] = key
            else:
                console.print(
                    f"  [yellow]⚠[/] No value entered; set it before `pulumi up`:\n"
                    f"  [dim]pulumi config set --path --secret nexus-provider-keys.{ref} <key>[/]"
                )

        return {
            "enabled": True,
            "byoc_env": byoc_env.strip(),
            "byoc_project_id": byoc_project_id,
            "storage_bucket_prefix": storage_bucket_prefix,
            "nexus_version": nexus_version.strip() or NEXUS_VERSION,
            "image_registry": image_registry.strip() or NEXUS_IMAGE_REGISTRY,
            "inference_base": inference_base.strip() or "https://api.pinecone.io",
            "inference_models_toml": inference_models_toml,
            "gemini_api_key": gemini_api_key,
            "provider_keys": provider_keys,
        }

    def _run_preflight_checks(
        self, project_id: str, region: str, zones: list[str], cidr: str
    ) -> bool:
        console.print()
        console.print(f"  {self._step('Preflight Checks')}")
        console.print()

        # IAM roles/owner is input-dependent (needs the project) and the
        # cloud-side checker never tests it -- run it here alongside the quota/
        # API/CIDR checks. BYOC creates IAM SAs + bindings, so roles/editor is
        # insufficient.
        iam_ok = preflight_checks.check_iam_owner(project_id)

        checker = GCPPreflightChecker(project_id, region, zones, cidr)
        cloud_ok = checker.run_checks()

        if not (iam_ok and cloud_ok):
            console.print()
            console.print(
                "  [red]Preflight checks failed. Fix the issues above before proceeding.[/]"
            )
            return False

        return True

    def _generate_project(
        self,
        output_dir: str,
        project_name: str,
        api_key: str,
        project_id: str,
        region: str,
        zones: list[str],
        cidr: str,
        deletion_protection: bool,
        public_access: bool,
        labels: dict[str, str],
        nexus: NexusWizardConfig | None = None,
    ):
        nexus = nexus if nexus is not None else NexusWizardConfig(enabled=False)
        console.print()

        if not self._check_pulumi_installed():
            console.print("  [red]✗[/] Pulumi CLI not found")
            console.print("  [dim]Install Pulumi first:[/] https://www.pulumi.com/docs/install/")
            return False

        # create Pulumi.yaml
        pulumi_yaml = {
            "name": project_name,
            "runtime": {
                "name": "python",
                "options": {"virtualenv": ".venv", "toolchain": "uv"},
            },
            "description": "Pinecone BYOC deployment on GCP",
        }

        os.makedirs(output_dir, exist_ok=True)
        pulumi_yaml_path = os.path.join(output_dir, "Pulumi.yaml")
        with open(pulumi_yaml_path, "w") as f:
            yaml.dump(pulumi_yaml, f, default_flow_style=False)
        console.print("  [green]✓[/] Created Pulumi.yaml")

        # create __main__.py
        main_py = '''"""Pinecone BYOC deployment on GCP."""

import pathlib

import pulumi
from pulumi_pinecone_byoc.gcp import PineconeGCPCluster, PineconeGCPClusterArgs
from pulumi_pinecone_byoc.common.nexus import NexusConfig

config = pulumi.Config()
gcp_config = pulumi.Config("gcp")

_nexus_enabled = config.get_bool("nexus-enabled")
# Inference-proxy model routing: the wizard-generated, customer-edited overlay
# next to this file. Shipped to the proxy as the `byoc` config profile.
_models_toml_path = pathlib.Path(__file__).parent / "inference-proxy-models.toml"
_nexus_models_toml = _models_toml_path.read_text() if _models_toml_path.exists() else None
# Nexus enabled => default the DB data plane to FDB so the two share one cluster (Nexus is an external client); explicit config wins.
_data_plane_backend = config.get("data-plane-backend") or ("fdb" if _nexus_enabled else "postgres")
_default_fdb_mode = "external" if _data_plane_backend == "fdb" else "single"
cluster = PineconeGCPCluster(
    "pinecone-byoc",
    PineconeGCPClusterArgs(
        pinecone_api_key=config.require_secret("pinecone-api-key"),
        pinecone_version=config.require("pinecone-version"),
        project=gcp_config.require("project"),
        region=config.require("region"),
        availability_zones=config.require_object("availability-zones"),
        vpc_cidr=config.get("vpc-cidr") or "10.112.0.0/12",
        deletion_protection=config.get_bool("deletion-protection") if config.get_bool("deletion-protection") is not None else True,
        public_access_enabled=config.get_bool("public-access-enabled") if config.get_bool("public-access-enabled") is not None else True,
        labels=config.get_object("labels") or {},
        data_plane_backend=_data_plane_backend,
        nexus=NexusConfig(
            version=config.get("nexus-version"),
            byoc_env=config.get("nexus-byoc-env"),
            image_registry=config.get("nexus-image-registry"),
            gemini_api_key=config.get_secret("nexus-gemini-api-key"),
            byoc_project_id=config.get("nexus-byoc-project-id"),
            byoc_vault_id=config.get("nexus-byoc-vault-id"),
            byoc_docs_api_url=config.get("nexus-byoc-docs-api-url"),
            storage_bucket_prefix=config.get("nexus-storage-bucket-prefix"),
            inference_models_toml=_nexus_models_toml,
            provider_keys=config.get_secret_object("nexus-provider-keys"),
            fdb_mode=config.get("nexus-fdb-mode") or _default_fdb_mode,
        ) if _nexus_enabled else None,
    ),
)

update_kubeconfig_command = cluster.name.apply(
    lambda name: f"gcloud container clusters get-credentials {name} --region {config.require(\'region\')} --project {gcp_config.require(\'project\')}"
)
pulumi.export("environment", cluster.environment.env_name)
pulumi.export("update_kubeconfig_command", update_kubeconfig_command)
if _nexus_enabled:
    pulumi.export("nexus_byoc_project_id", cluster.nexus_byoc_project_id)
    pulumi.export("nexus_byoc_session_credential", cluster.nexus_byoc_session_credential)
    pulumi.export(
        "nexus_default_workspace_data_console_url",
        cluster.nexus_default_workspace_data_console_url,
    )
    pulumi.export(
        "nexus_default_workspace_control_console_url",
        cluster.nexus_default_workspace_control_console_url,
    )
if config.get_bool("public-access-enabled") is False:
    pulumi.export("psc_service_attachment", cluster.psc_service_attachment)
'''

        main_py_path = os.path.join(output_dir, "__main__.py")
        with open(main_py_path, "w") as f:
            f.write(main_py)
        console.print("  [green]✓[/] Created __main__.py")

        # create pyproject.toml for uv toolchain to install dependencies
        pyproject_content = """[project]
name = "pinecone-byoc"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pulumi-pinecone-nexus-byoc[gcp]"]
"""
        if self._local_package_path:
            pyproject_content += (
                "\n[tool.uv.sources]\n"
                f'pulumi-pinecone-nexus-byoc = {{ path = "{self._local_package_path}", editable = true }}\n'
            )
        pyproject_path = os.path.join(output_dir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(pyproject_content)
        console.print("  [green]✓[/] Created pyproject.toml")

        # Nexus inference-proxy model-routing config. Written only when Nexus is
        # enabled; __main__.py reads it into NexusConfig.inference_models_toml.
        # Use the wizard/headless-built catalog when present, else the editable
        # default template.
        if nexus.get("enabled"):
            models_path = os.path.join(output_dir, NEXUS_INFERENCE_MODELS_FILENAME)
            with open(models_path, "w") as f:
                inference_models_toml = nexus.get("inference_models_toml")
                # None => no catalog built, write the editable default template.
                # A non-str value here would be a bug; let f.write surface it.
                f.write(
                    inference_models_toml
                    if inference_models_toml is not None
                    else NEXUS_INFERENCE_MODELS_TEMPLATE
                )
            console.print(f"  [green]✓[/] Created {NEXUS_INFERENCE_MODELS_FILENAME}")

        # create stack config
        stack_name = self._stack_name
        deletion_protection_str = str(deletion_protection).lower()
        public_access_str = str(public_access).lower()
        config_content = f"""config:
  gcp:project: {project_id}
  {project_name}:region: {region}
  {project_name}:pinecone-version: {PINECONE_VERSION}
  {project_name}:vpc-cidr: {cidr}
  {project_name}:deletion-protection: {deletion_protection_str}
  {project_name}:public-access-enabled: {public_access_str}
  {project_name}:availability-zones:
"""
        for zone in zones:
            config_content += f"    - {zone}\n"

        # add labels if provided (quote values to handle YAML special chars)
        if labels:
            config_content += f"  {project_name}:labels:\n"
            for key, value in labels.items():
                config_content += f'    {key}: "{value}"\n'

        # Nexus BYOC install (task 2.7). Written only when enabled, so DB-only
        # stacks omit these keys entirely and `nexus_enabled` stays False.
        if nexus.get("enabled"):
            config_content += f"  {project_name}:nexus-enabled: true\n"
            config_content += (
                f"  {project_name}:nexus-version: {nexus.get('nexus_version', NEXUS_VERSION)}\n"
            )
            config_content += (
                f"  {project_name}:nexus-image-registry: "
                f"{nexus.get('image_registry', NEXUS_IMAGE_REGISTRY)}\n"
            )
            if nexus.get("byoc_env"):
                config_content += f"  {project_name}:nexus-byoc-env: {nexus['byoc_env']}\n"
            if nexus.get("byoc_project_id"):
                config_content += (
                    f"  {project_name}:nexus-byoc-project-id: {nexus['byoc_project_id']}\n"
                )
            config_content += (
                f"  {project_name}:nexus-inference-base: "
                f"{nexus.get('inference_base', 'https://api.pinecone.io')}\n"
            )
            if nexus.get("storage_bucket_prefix"):
                config_content += (
                    f"  {project_name}:nexus-storage-bucket-prefix: "
                    f"{nexus['storage_bucket_prefix']}\n"
                )
            # nexus-gemini-api-key / nexus-provider-keys.* are secrets; the
            # wizard sets them itself in the secret-setting step below (mirroring
            # pinecone-api-key), so they are intentionally omitted from this
            # plaintext stack config.

        config_path = os.path.join(output_dir, f"Pulumi.{stack_name}.yaml")
        with open(config_path, "w") as f:
            f.write(config_content)
        console.print(f"  [green]✓[/] Created Pulumi.{stack_name}.yaml")

        if self._skip_install:
            return True

        # install dependencies with uv
        with Status("  [dim]Installing dependencies...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                ["uv", "sync"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            # get installed version
            version_result = subprocess.run(
                ["uv", "pip", "show", "pulumi-pinecone-nexus-byoc"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )
            pkg_version = "unknown"
            for line in version_result.stdout.splitlines():
                if line.startswith("Version:"):
                    pkg_version = line.split(":", 1)[1].strip()
                    break
            console.print(
                f"  [green]✓[/] Dependencies installed [dim](pulumi-pinecone-nexus-byoc v{pkg_version})[/]"
            )
        else:
            console.print(f"  [red]✗[/] Failed to install dependencies: {result.stderr.strip()}")
            console.print("  [dim]Run manually:[/] uv sync")
            return False

        # init stack
        with Status("  [dim]Initializing stack...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "stack",
                    "select",
                    "--create",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            console.print(f"  [green]✓[/] Stack {stack_name} ready")
        else:
            console.print(f"  [yellow]⚠[/] Stack init: {result.stderr.strip()}")

        # set api key as secret
        with Status("  [dim]Storing API key securely...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "config",
                    "set",
                    "--secret",
                    "pinecone-api-key",
                    api_key,
                    "--stack",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            console.print(f"  [red]✗[/] Failed to store API key: {result.stderr.strip()}")
            console.print(
                "  [dim]Run manually:[/] pulumi config set --secret pinecone-api-key <key>"
            )
            return False

        console.print("  [green]✓[/] API key stored securely")

        # nexus-gemini-api-key is read by NexusConfig; nexus-provider-keys.<ref>
        # by the inference proxy (default catalog's api_key_ref is `gemini-api-key`,
        # so both are set from the same key).
        if nexus.get("enabled"):
            gemini_api_key = nexus.get("gemini_api_key")
            provider_keys = dict(nexus.get("provider_keys") or {})
            if gemini_api_key:
                provider_keys.setdefault("gemini-api-key", gemini_api_key)

            def _set_secret(config_args: list[str], value: str, label: str) -> None:
                with Status(f"  [dim]Storing {label}...[/]", console=console, spinner="dots"):
                    res = subprocess.run(
                        [
                            "pulumi",
                            "config",
                            "set",
                            *config_args,
                            value,
                            "--stack",
                            stack_name,
                            "--cwd",
                            output_dir,
                        ],
                        capture_output=True,
                        text=True,
                    )
                if res.returncode != 0:
                    console.print(f"  [red]✗[/] Failed to store {label}: {res.stderr.strip()}")
                    console.print(
                        f"  [dim]Run manually:[/] pulumi config set {' '.join(config_args)} <key>"
                    )
                else:
                    console.print(f"  [green]✓[/] {label} stored securely")

            if gemini_api_key:
                _set_secret(["--secret", "nexus-gemini-api-key"], gemini_api_key, "Gemini API key")
            for ref, value in provider_keys.items():
                _set_secret(
                    ["--path", "--secret", f"nexus-provider-keys.{ref}"],
                    value,
                    f"provider key ({ref})",
                )

        self._print_success(output_dir)
        return True


# ---------------------------------------------------------------------------
# Azure Setup Wizard
# ---------------------------------------------------------------------------


class AzurePreflightChecker:
    def __init__(self, subscription_id: str, region: str, zones: list[str], cidr: str):
        self.subscription_id = subscription_id
        self.region = region
        self.zones = zones
        self.cidr = cidr
        self.results: list[PreflightResult] = []

    def run_checks(self) -> bool:
        checks = [
            ("Resource Providers", self._check_resource_providers),
            ("PostgreSQL Flexible Server", self._check_postgres_availability),
            ("vCPU Quota", self._check_vcpu_quota),
            ("AKS Clusters", self._check_aks_quota),
            ("VM SKUs", self._check_vm_skus),
            ("Availability Zones", self._check_zones),
            ("VNet CIDR", self._check_cidr_conflicts),
        ]

        for name, check_fn in checks:
            with Status(f"  [dim]Checking {name}...[/]", console=console, spinner="dots"):
                check_fn()

            r = self.results[-1]
            status = "✓" if r.passed else "✗"
            color = "green" if r.passed else "red"
            console.print(f"  [{color}]{status}[/] {r.name}: {r.message}")
            if r.details and not r.passed:
                console.print(f"    [dim]{r.details}[/]")

        failed = [r for r in self.results if not r.passed]
        return len(failed) == 0

    def _add_result(self, name: str, passed: bool, message: str, details: str | None = None):
        result = PreflightResult(name, passed, message, details)
        self.results.append(result)

    def _az_json(self, args: list[str]):
        import json

        result = subprocess.run(
            ["az"] + args + ["--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip().split("\n")[0])
        return json.loads(result.stdout)

    def _check_resource_providers(self):
        required_providers = [
            "Microsoft.Compute",
            "Microsoft.ContainerService",
            "Microsoft.DBforPostgreSQL",
            "Microsoft.Storage",
            "Microsoft.Network",
            "Microsoft.KeyVault",
            "Microsoft.ManagedIdentity",
            "Microsoft.Authorization",
        ]

        try:
            providers = self._az_json(["provider", "list"])
            registered = {
                p["namespace"] for p in providers if p.get("registrationState") == "Registered"
            }
            missing = [p for p in required_providers if p not in registered]

            if missing:
                self._add_result(
                    "Resource Providers",
                    False,
                    f"{len(missing)} not registered: {', '.join(missing)}",
                    f"Run: az provider register --namespace {missing[0]}",
                )
            else:
                self._add_result(
                    "Resource Providers",
                    True,
                    f"All {len(required_providers)} required providers registered",
                )
        except Exception as e:
            self._add_result("Resource Providers", False, f"Failed to check: {e}")

    def _check_postgres_availability(self):
        try:
            result = subprocess.run(
                [
                    "az",
                    "postgres",
                    "flexible-server",
                    "list-skus",
                    "--location",
                    self.region,
                    "--subscription",
                    self.subscription_id,
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self._add_result(
                    "PostgreSQL Flexible Server",
                    False,
                    f"Failed: {result.stderr.strip().split(chr(10))[0]}",
                )
                return

            import json

            skus = json.loads(result.stdout)
            if not skus:
                self._add_result(
                    "PostgreSQL Flexible Server",
                    False,
                    f"No SKUs available in {self.region}",
                    "Choose a different region",
                )
                return

            # check if provisioning is restricted in this region
            reason = skus[0].get("reason") or ""
            if "restricted" in reason.lower():
                self._add_result(
                    "PostgreSQL Flexible Server",
                    False,
                    f"Provisioning restricted in {self.region}",
                    "Choose a different region or request a quota increase",
                )
                return

            # check that our target SKU (Standard_D2s_v3) is available
            target_sku = "Standard_D2s_v3"
            found = False
            for cap in skus:
                for edition in cap.get("supportedServerEditions", []):
                    if edition.get("name") == "GeneralPurpose":
                        for sku in edition.get("supportedServerSkus", []):
                            if sku.get("name") == target_sku:
                                found = True
                                break

            if found:
                self._add_result(
                    "PostgreSQL Flexible Server",
                    True,
                    f"{target_sku} available in {self.region}",
                )
            else:
                self._add_result(
                    "PostgreSQL Flexible Server",
                    False,
                    f"{target_sku} not available in {self.region}",
                    "Choose a different region or VM SKU",
                )
        except Exception as e:
            self._add_result("PostgreSQL Flexible Server", False, f"Failed to check: {e}")

    def _check_vcpu_quota(self):
        try:
            usages = self._az_json(
                [
                    "vm",
                    "list-usage",
                    "--location",
                    self.region,
                    "--subscription",
                    self.subscription_id,
                ]
            )
            # check total regional vCPUs
            for usage in usages:
                if usage.get("name", {}).get("value") == "cores":
                    current = int(usage.get("currentValue", 0))
                    limit = int(usage.get("limit", 0))
                    available = limit - current
                    # need at least 8 vCPUs for default node pool
                    self._add_result(
                        "vCPU Quota",
                        available >= 8,
                        f"{available} available [dim](using {current}/{limit})[/]",
                        "Request quota increase for 'Total Regional vCPUs'"
                        if available < 8
                        else None,
                    )
                    return
            self._add_result("vCPU Quota", True, "Could not determine quota, skipping")
        except Exception as e:
            self._add_result("vCPU Quota", False, f"Failed to check: {e}")

    def _check_aks_quota(self):
        try:
            clusters = self._az_json(
                [
                    "aks",
                    "list",
                    "--subscription",
                    self.subscription_id,
                ]
            )
            current = len(clusters) if isinstance(clusters, list) else 0
            quota = 100
            available = quota - current
            self._add_result(
                "AKS Clusters",
                available >= 1,
                f"{available} available [dim](using {current}/{quota})[/]",
            )
        except Exception as e:
            self._add_result("AKS Clusters", False, f"Failed to check: {e}")

    def _check_vm_skus(self):
        vm_skus = [
            "Standard_D4s_v7",
            "Standard_L2aos_v4",
            "Standard_L2s_v4",
            "Standard_L4s_v4",
        ]
        try:
            import json

            result = subprocess.run(
                [
                    "az",
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"https://management.azure.com/subscriptions/{self.subscription_id}"
                    f"/providers/Microsoft.Compute/skus?api-version=2021-07-01"
                    f"&$filter=location eq '{self.region}'",
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self._add_result("VM SKUs", False, f"Failed: {result.stderr.strip()}")
                return

            data = json.loads(result.stdout)
            available_skus: set[str] = set()
            for sku in data.get("value", []):
                if sku.get("resourceType") != "virtualMachines":
                    continue
                sku_name = sku.get("name", "")
                restrictions = sku.get("restrictions", [])
                is_restricted = any(r.get("type") == "Location" for r in restrictions)
                if not is_restricted:
                    available_skus.add(sku_name)

            unavailable = [s for s in vm_skus if s not in available_skus]
            self._add_result(
                "VM SKUs",
                len(unavailable) == 0,
                "All required SKUs available"
                if not unavailable
                else f"Unavailable: {', '.join(unavailable)}",
                "Choose a different region" if unavailable else None,
            )
        except Exception as e:
            self._add_result("VM SKUs", False, f"Failed to check: {e}")

    def _check_zones(self):
        try:
            import json

            # use REST API directly - much faster than `az vm list-skus` CLI
            result = subprocess.run(
                [
                    "az",
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"https://management.azure.com/subscriptions/{self.subscription_id}"
                    f"/providers/Microsoft.Compute/skus?api-version=2021-07-01"
                    f"&$filter=location eq '{self.region}'",
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self._add_result("Availability Zones", False, f"Failed: {result.stderr.strip()}")
                return

            data = json.loads(result.stdout)
            required_skus = [
                "Standard_D4s_v7",
                "Standard_L2aos_v4",
                "Standard_L2s_v4",
                "Standard_L4s_v4",
            ]
            per_sku_zones: dict[str, set[str]] = {}
            for sku in data.get("value", []):
                if (
                    sku.get("resourceType") != "virtualMachines"
                    or sku.get("name") not in required_skus
                ):
                    continue
                sku_name = sku["name"]
                zones_for_sku = per_sku_zones.setdefault(sku_name, set())
                for loc in sku.get("locationInfo", []):
                    zones_for_sku.update(loc.get("zones", []))
                for restriction in sku.get("restrictions", []):
                    if restriction.get("type") == "Zone":
                        rz = restriction.get("restrictionInfo", {}).get("zones", [])
                        zones_for_sku -= set(rz)

            available_zones = None
            for sku_zones in per_sku_zones.values():
                available_zones = (
                    sku_zones if available_zones is None else available_zones & sku_zones
                )

            if not available_zones:
                missing = [s for s in required_skus if s not in per_sku_zones]
                self._add_result(
                    "Availability Zones",
                    False,
                    "No zones available for all required SKUs"
                    + (f" (missing: {', '.join(missing)})" if missing else ""),
                )
                return

            invalid = [z for z in self.zones if z not in available_zones]
            if invalid:
                self._add_result(
                    "Availability Zones",
                    False,
                    f"Zones not available: {', '.join(invalid)}",
                    f"Valid zones for {self.region}: {', '.join(sorted(available_zones))}",
                )
            else:
                self._add_result("Availability Zones", True, "All requested zones available")
        except Exception as e:
            self._add_result("Availability Zones", False, f"Failed to check: {e}")

    def _check_cidr_conflicts(self):
        import ipaddress

        try:
            aks_net = ipaddress.ip_network(self.cidr)
        except ValueError:
            self._add_result(
                "VNet CIDR",
                False,
                f"Invalid CIDR: {self.cidr}",
                "Enter a valid CIDR block (e.g., 10.0.0.0/16)",
            )
            return

        if aks_net.prefixlen > 20:
            self._add_result(
                "VNet CIDR",
                False,
                f"CIDR /{aks_net.prefixlen} is too small (minimum is /20)",
                "Use a /20 or larger CIDR block (e.g., 10.0.0.0/16) to ensure enough IP addresses for node scaling.",
            )
            return

        # derive subnets the same way vnet.py does
        try:
            db_net = ipaddress.ip_network(
                f"{aks_net.network_address + aks_net.num_addresses}/{aks_net.prefixlen}"
            )
            pls_net = ipaddress.ip_network(f"{db_net.network_address + db_net.num_addresses}/27")
        except ValueError:
            self._add_result(
                "VNet CIDR",
                False,
                f"CIDR {self.cidr} is too small to derive required subnets",
                "Use a /16 or larger CIDR block (e.g., 10.0.0.0/16)",
            )
            return
        aks_service_cidr = ipaddress.ip_network("112.0.0.0/16")

        # check derived subnets don't overlap AKS service CIDR
        all_nets = [("AKS subnet", aks_net), ("DB subnet", db_net), ("PLS subnet", pls_net)]
        service_conflicts = []
        for label, net in all_nets:
            if net.overlaps(aks_service_cidr):
                service_conflicts.append(f"{label} ({net})")
        if service_conflicts:
            self._add_result(
                "VNet CIDR",
                False,
                f"Overlaps AKS service CIDR 112.0.0.0/16: {', '.join(service_conflicts)}",
                "Choose a CIDR that doesn't overlap 112.0.0.0/16",
            )
            return

        try:
            # Enumerate existing VNets subscription-wide via the ARM REST API.
            # `az network vnet list` cannot list across an entire subscription on
            # recent Azure CLI versions (the migrated `aaz` module marks
            # --resource-group as required), so use `az rest`, which is built into
            # the CLI core and supports subscription-wide listing with pagination.
            vnets = []
            url = (
                "https://management.azure.com/subscriptions/"
                f"{self.subscription_id}/providers/Microsoft.Network/"
                "virtualNetworks?api-version=2023-09-01"
            )
            while url:
                resp = self._az_json(["rest", "--method", "get", "--url", url])
                if not isinstance(resp, dict):
                    break
                vnets.extend(resp.get("value", []) or [])
                url = resp.get("nextLink")

            # check all derived subnets against existing VNets
            check_nets = [aks_net, db_net, pls_net]
            conflicts = []
            for vnet in vnets:
                address_space = vnet.get("properties", {}).get("addressSpace", {})
                for prefix in address_space.get("addressPrefixes", []):
                    try:
                        existing_net = ipaddress.ip_network(prefix)
                        for net in check_nets:
                            if net.overlaps(existing_net):
                                conflicts.append(f"{net} overlaps {prefix}")
                    except ValueError:
                        continue

            if conflicts:
                self._add_result(
                    "VNet CIDR",
                    False,
                    f"Conflicts with existing VNets: {', '.join(conflicts)}",
                    "Choose a non-overlapping CIDR block",
                )
            else:
                self._add_result("VNet CIDR", True, f"{self.cidr} has no conflicts")
        except Exception as e:
            self._add_result("VNet CIDR", False, f"Failed to check: {e}")


class AzureSetupWizard(BaseSetupWizard):
    HEADER_TITLE = "Pinecone BYOC Setup Wizard - Azure"
    HEADER_SUBTITLE = (
        "This wizard will set up everything you need to deploy Pinecone BYOC on Azure."
    )
    DEFAULT_CIDR = "10.0.0.0/16"
    DELETION_PROTECTION_DESC = (
        "Protect PostgreSQL databases and storage accounts from accidental deletion"
    )
    PRIVATE_ACCESS_DESC = "Private access requires Azure Private Link (more secure)"
    METADATA_NAME = "tags"
    CLOUD_NAME = "Azure"

    def run(self, output_dir: str = ".") -> bool:
        if self._headless:
            return self._run_headless(output_dir)

        self._print_header()

        api_key = self._get_api_key()
        if not api_key:
            return False

        if not self._validate_api_key(api_key):
            return False

        subscription_id = self._validate_azure_creds()
        if not subscription_id:
            return False

        subscription_id = self._get_subscription_id(subscription_id)
        region = self._get_region()
        zones = self._get_zones(subscription_id, region)
        cidr = self._get_cidr()
        deletion_protection = self._get_deletion_protection()
        public_access = self._get_public_access()
        tags = self._get_custom_metadata()

        if not self._run_preflight_checks(subscription_id, region, zones, cidr):
            return False

        project_name = self._get_project_name()

        if not self._setup_pulumi_backend():
            return False

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            subscription_id,
            region,
            zones,
            cidr,
            deletion_protection,
            public_access,
            tags,
        )

    def _run_headless(self, output_dir: str) -> bool:
        console.print("  [dim]Running in headless mode (reading from environment)[/]")

        api_key = os.environ.get("PINECONE_API_KEY")
        if not api_key:
            console.print("  [red]✗[/] PINECONE_API_KEY environment variable is required")
            return False

        subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            console.print("  [red]✗[/] AZURE_SUBSCRIPTION_ID environment variable is required")
            return False

        region = os.environ.get("PINECONE_REGION", "eastus")
        zones_str = os.environ.get("PINECONE_AZS", "1,2")
        zones = [z.strip() for z in zones_str.split(",") if z.strip()]
        cidr = os.environ.get("PINECONE_VPC_CIDR", self.DEFAULT_CIDR)
        deletion_protection = (
            os.environ.get("PINECONE_DELETION_PROTECTION", "true").lower() == "true"
        )
        public_access = os.environ.get("PINECONE_PUBLIC_ACCESS", "true").lower() == "true"
        project_name = os.environ.get("PINECONE_PROJECT_NAME", "pinecone-byoc")

        # Nexus is opt-in; default off so headless DB-only installs are unchanged.
        # Uses the SAME env var names as the GCP wizard, except the image registry
        # defaults to the Azure ACR `nexus` repo.
        if os.environ.get("PINECONE_NEXUS_ENABLED", "false").lower() == "true":
            # The Pinecone gCPS project UUID the BYOC vault belongs to (matched
            # against projects.id by CPGW). Required, and must NOT be the cloud
            # project/subscription -- pass it as its own var.
            byoc_project_id = os.environ.get("PINECONE_BYOC_PROJECT_ID", "").strip()
            if not byoc_project_id:
                console.print(
                    "  [red]✗[/] PINECONE_BYOC_PROJECT_ID environment variable is required"
                    " when Nexus is enabled (the Pinecone gCPS project UUID)"
                )
                return False
            if not _is_uuid(byoc_project_id):
                console.print(
                    "  [red]✗[/] PINECONE_BYOC_PROJECT_ID must be a Pinecone gCPS project"
                    " UUID (e.g. 123e4567-e89b-12d3-a456-426614174000)"
                )
                return False
            nexus: NexusWizardConfig = {
                "enabled": True,
                "byoc_env": os.environ.get("PINECONE_BYOC_ENV", ""),
                "nexus_version": os.environ.get("PINECONE_NEXUS_VERSION", NEXUS_VERSION),
                "image_registry": os.environ.get(
                    "PINECONE_NEXUS_IMAGE_REGISTRY", NEXUS_AZURE_IMAGE_REGISTRY
                ),
                "inference_base": os.environ.get(
                    "PINECONE_INFERENCE_BASE", "https://api.pinecone.io"
                ),
                "byoc_project_id": byoc_project_id,
                # Opt-in blob backend: unset = fs (PVC); set = provision blob containers.
                "storage_bucket_prefix": os.environ.get("PINECONE_NEXUS_STORAGE_BUCKET_PREFIX", ""),
                # Inference models from env JSON, or None -> default template.
                "inference_models_toml": self._headless_inference_models_toml(),
            }
        else:
            nexus = NexusWizardConfig(enabled=False)

        return self._generate_project(
            output_dir,
            project_name,
            api_key,
            subscription_id,
            region,
            zones,
            cidr,
            deletion_protection,
            public_access,
            {},
            nexus,
        )

    def _validate_azure_creds(self) -> str | None:
        console.print()
        console.print(f"  {self._step('Azure Credentials')}")
        console.print()

        with Status("  [dim]Validating Azure credentials...[/]", console=console, spinner="dots"):
            try:
                import json

                result = subprocess.run(
                    ["az", "account", "show", "--output", "json"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    account = json.loads(result.stdout)
                    subscription_id = account.get("id", "")
                    subscription_name = account.get("name", "")
                    console.print(
                        f"  [green]✓[/] Azure credentials valid "
                        f"[dim](Subscription: {subscription_name} / {subscription_id})[/]"
                    )
                    return subscription_id
                else:
                    raise Exception("Could not determine Azure subscription")

            except Exception as e:
                console.print(f"  [red]✗[/] Azure credentials invalid: {e}")
                console.print()
                console.print("  [dim]Make sure you have valid Azure credentials configured.[/]")
                console.print("  [dim]You can set them via:[/]")
                console.print("    [dim]· az login[/]")
                console.print("    [dim]· az account set --subscription SUBSCRIPTION_ID[/]")
                console.print("    [dim]· AZURE_SUBSCRIPTION_ID environment variable[/]")
                return None

    def _get_subscription_id(self, detected_subscription: str) -> str:
        console.print()
        console.print(f"  {self._step('Azure Subscription ID')}")
        console.print()
        return self._prompt("Enter Azure subscription ID", detected_subscription)

    def _get_region(self) -> str:
        console.print()
        console.print(f"  {self._step('Azure Region')}")
        console.print()
        return self._prompt("Enter Azure region", "eastus")

    def _fetch_zones(self, subscription_id: str, region: str) -> list[str]:
        import json as _json

        try:
            result = subprocess.run(
                [
                    "az",
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"https://management.azure.com/subscriptions/{subscription_id}"
                    f"/providers/Microsoft.Compute/skus?api-version=2021-07-01"
                    f"&$filter=location eq '{region}'",
                    "--output",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                data = _json.loads(result.stdout)
                required_skus = [
                    "Standard_D4s_v7",
                    "Standard_L2aos_v4",
                    "Standard_L2s_v4",
                    "Standard_L4s_v4",
                ]
                per_sku_zones: dict[str, set[str]] = {}
                for sku in data.get("value", []):
                    if (
                        sku.get("resourceType") != "virtualMachines"
                        or sku.get("name") not in required_skus
                    ):
                        continue
                    sku_name = sku["name"]
                    zones_for_sku = per_sku_zones.setdefault(sku_name, set())
                    for loc in sku.get("locationInfo", []):
                        zones_for_sku.update(loc.get("zones", []))
                    for restriction in sku.get("restrictions", []):
                        if restriction.get("type") == "Zone":
                            rz = restriction.get("restrictionInfo", {}).get("zones", [])
                            zones_for_sku -= set(rz)
                # intersect: only zones where ALL required SKUs are available
                available = None
                for sku_zones in per_sku_zones.values():
                    available = sku_zones if available is None else available & sku_zones
                if available:
                    return sorted(available)
        except Exception as e:
            console.print(f"  [yellow]⚠[/] Could not fetch zones from Azure: {e}")
        return ["1", "2", "3"]

    def _get_zones(self, subscription_id: str, region: str) -> list[str]:
        console.print()
        console.print(f"  {self._step('Availability Zones')}")
        console.print()

        with Status("  [dim]Fetching availability zones...[/]", console=console, spinner="dots"):
            available = self._fetch_zones(subscription_id, region)

        console.print(f"  [dim]Available in {region}:[/] {', '.join(available)}")
        default_zones = available[:2]

        zones_input = self._prompt("Enter zones (comma-separated)", ",".join(default_zones))
        zones = [zone.strip() for zone in zones_input.split(",")]
        return zones

    def _run_preflight_checks(
        self, subscription_id: str, region: str, zones: list[str], cidr: str
    ) -> bool:
        console.print()
        console.print(f"  {self._step('Preflight Checks')}")
        console.print()

        checker = AzurePreflightChecker(subscription_id, region, zones, cidr)
        if not checker.run_checks():
            console.print()
            console.print(
                "  [red]Preflight checks failed. Fix the issues above before proceeding.[/]"
            )
            return False

        return True

    def _generate_project(
        self,
        output_dir: str,
        project_name: str,
        api_key: str,
        subscription_id: str,
        region: str,
        zones: list[str],
        cidr: str,
        deletion_protection: bool,
        public_access: bool,
        tags: dict[str, str],
        nexus: NexusWizardConfig | None = None,
    ):
        nexus = nexus if nexus is not None else NexusWizardConfig(enabled=False)
        console.print()

        if not self._check_pulumi_installed():
            console.print("  [red]✗[/] Pulumi CLI not found")
            console.print("  [dim]Install Pulumi first:[/] https://www.pulumi.com/docs/install/")
            return False

        pulumi_yaml = {
            "name": project_name,
            "runtime": {
                "name": "python",
                "options": {"virtualenv": ".venv", "toolchain": "uv"},
            },
            "description": "Pinecone BYOC deployment on Azure",
        }

        os.makedirs(output_dir, exist_ok=True)
        pulumi_yaml_path = os.path.join(output_dir, "Pulumi.yaml")
        with open(pulumi_yaml_path, "w") as f:
            yaml.dump(pulumi_yaml, f, default_flow_style=False)
        console.print("  [green]✓[/] Created Pulumi.yaml")

        main_py = '''"""Pinecone BYOC deployment on Azure."""

import pathlib

import pulumi
from pulumi_pinecone_byoc.azure import PineconeAzureCluster, PineconeAzureClusterArgs
from pulumi_pinecone_byoc.common.nexus import NexusConfig

config = pulumi.Config()

_nexus_enabled = config.get_bool("nexus-enabled")
# Inference-proxy model routing: the wizard-generated, customer-edited overlay
# next to this file. Shipped to the proxy as the `byoc` config profile.
_models_toml_path = pathlib.Path(__file__).parent / "inference-proxy-models.toml"
_nexus_models_toml = _models_toml_path.read_text() if _models_toml_path.exists() else None
cluster = PineconeAzureCluster(
    "pinecone-byoc",
    PineconeAzureClusterArgs(
        pinecone_api_key=config.require_secret("pinecone-api-key"),
        pinecone_version=config.require("pinecone-version"),
        subscription_id=config.require("subscription-id"),
        region=config.require("region"),
        availability_zones=config.require_object("availability-zones"),
        vpc_cidr=config.get("vpc-cidr") or "10.0.0.0/16",
        deletion_protection=config.get_bool("deletion-protection") if config.get_bool("deletion-protection") is not None else True,
        public_access_enabled=config.get_bool("public-access-enabled") if config.get_bool("public-access-enabled") is not None else True,
        tags=config.get_object("tags"),
        nexus=NexusConfig(
            version=config.get("nexus-version"),
            byoc_env=config.get("nexus-byoc-env"),
            image_registry=config.get("nexus-image-registry"),
            gemini_api_key=config.get_secret("nexus-gemini-api-key"),
            byoc_project_id=config.get("nexus-byoc-project-id"),
            byoc_vault_id=config.get("nexus-byoc-vault-id"),
            byoc_docs_api_url=config.get("nexus-byoc-docs-api-url"),
            storage_bucket_prefix=config.get("nexus-storage-bucket-prefix"),
            inference_models_toml=_nexus_models_toml,
            provider_keys=config.get_secret_object("nexus-provider-keys"),
        ) if _nexus_enabled else None,
    ),
)

region = config.require("region")
update_kubeconfig_command = cluster.name.apply(
    lambda name: f"az aks get-credentials --resource-group {name.removeprefix(\'cluster-\')}-{region}-rg --name {name}"
)
pulumi.export("environment", cluster.environment.env_name)
pulumi.export("update_kubeconfig_command", update_kubeconfig_command)
if _nexus_enabled:
    pulumi.export("nexus_byoc_project_id", cluster.nexus_byoc_project_id)
    pulumi.export("nexus_byoc_session_credential", cluster.nexus_byoc_session_credential)
if config.get_bool("public-access-enabled") is False:
    pulumi.export("private_link_service_name", cluster.private_link_service_name)
    pulumi.export("private_link_service_resource_group", cluster.private_link_service_resource_group)
'''

        main_py_path = os.path.join(output_dir, "__main__.py")
        with open(main_py_path, "w") as f:
            f.write(main_py)
        console.print("  [green]✓[/] Created __main__.py")

        pyproject_content = """[project]
name = "pinecone-byoc"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pulumi-pinecone-nexus-byoc[azure]"]
"""
        if self._local_package_path:
            pyproject_content += (
                "\n[tool.uv.sources]\n"
                f'pulumi-pinecone-nexus-byoc = {{ path = "{self._local_package_path}", editable = true }}\n'
            )
        pyproject_path = os.path.join(output_dir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(pyproject_content)
        console.print("  [green]✓[/] Created pyproject.toml")

        # Nexus inference-proxy model-routing config. Written only when Nexus is
        # enabled; __main__.py reads it into NexusConfig.inference_models_toml.
        # Use the wizard/headless-built catalog when present, else the editable
        # default template.
        if nexus.get("enabled"):
            models_path = os.path.join(output_dir, NEXUS_INFERENCE_MODELS_FILENAME)
            with open(models_path, "w") as f:
                inference_models_toml = nexus.get("inference_models_toml")
                # None => no catalog built, write the editable default template.
                # A non-str value here would be a bug; let f.write surface it.
                f.write(
                    inference_models_toml
                    if inference_models_toml is not None
                    else NEXUS_INFERENCE_MODELS_TEMPLATE
                )
            console.print(f"  [green]✓[/] Created {NEXUS_INFERENCE_MODELS_FILENAME}")

        stack_name = self._stack_name
        deletion_protection_str = str(deletion_protection).lower()
        public_access_str = str(public_access).lower()
        config_content = f"""config:
  {project_name}:subscription-id: {subscription_id}
  {project_name}:region: {region}
  {project_name}:pinecone-version: {PINECONE_VERSION}
  {project_name}:vpc-cidr: {cidr}
  {project_name}:deletion-protection: {deletion_protection_str}
  {project_name}:public-access-enabled: {public_access_str}
  {project_name}:availability-zones:
"""
        for zone in zones:
            config_content += f'    - "{zone}"\n'

        if tags:
            config_content += f"  {project_name}:tags:\n"
            for key, value in tags.items():
                config_content += f'    {key}: "{value}"\n'

        # Nexus BYOC install. Written only when enabled, so DB-only stacks omit
        # these keys entirely and `nexus_enabled` stays False. Mirrors the GCP
        # wizard.
        if nexus.get("enabled"):
            config_content += f"  {project_name}:nexus-enabled: true\n"
            config_content += (
                f"  {project_name}:nexus-version: {nexus.get('nexus_version', NEXUS_VERSION)}\n"
            )
            config_content += (
                f"  {project_name}:nexus-image-registry: "
                f"{nexus.get('image_registry', NEXUS_AZURE_IMAGE_REGISTRY)}\n"
            )
            if nexus.get("byoc_env"):
                config_content += f"  {project_name}:nexus-byoc-env: {nexus['byoc_env']}\n"
            if nexus.get("byoc_project_id"):
                config_content += (
                    f"  {project_name}:nexus-byoc-project-id: {nexus['byoc_project_id']}\n"
                )
            config_content += (
                f"  {project_name}:nexus-inference-base: "
                f"{nexus.get('inference_base', 'https://api.pinecone.io')}\n"
            )
            if nexus.get("storage_bucket_prefix"):
                config_content += (
                    f"  {project_name}:nexus-storage-bucket-prefix: "
                    f"{nexus['storage_bucket_prefix']}\n"
                )
            # nexus-gemini-api-key is a secret; set it out-of-band:
            #   pulumi config set --secret <project>:nexus-gemini-api-key <key>

        config_path = os.path.join(output_dir, f"Pulumi.{stack_name}.yaml")
        with open(config_path, "w") as f:
            f.write(config_content)
        console.print(f"  [green]✓[/] Created Pulumi.{stack_name}.yaml")

        if self._skip_install:
            return True

        with Status("  [dim]Installing dependencies...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                ["uv", "sync"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            version_result = subprocess.run(
                ["uv", "pip", "show", "pulumi-pinecone-nexus-byoc"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )
            pkg_version = "unknown"
            for line in version_result.stdout.splitlines():
                if line.startswith("Version:"):
                    pkg_version = line.split(":", 1)[1].strip()
                    break
            console.print(
                f"  [green]✓[/] Dependencies installed "
                f"[dim](pulumi-pinecone-nexus-byoc v{pkg_version})[/]"
            )
        else:
            console.print(f"  [red]✗[/] Failed to install dependencies: {result.stderr.strip()}")
            console.print("  [dim]Run manually:[/] uv sync")
            return False

        with Status("  [dim]Initializing stack...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "stack",
                    "select",
                    "--create",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode == 0:
            console.print(f"  [green]✓[/] Stack {stack_name} ready")
        else:
            console.print(f"  [yellow]⚠[/] Stack init: {result.stderr.strip()}")

        with Status("  [dim]Storing API key securely...[/]", console=console, spinner="dots"):
            result = subprocess.run(
                [
                    "pulumi",
                    "config",
                    "set",
                    "--secret",
                    "pinecone-api-key",
                    api_key,
                    "--stack",
                    stack_name,
                    "--cwd",
                    output_dir,
                ],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            console.print(f"  [red]✗[/] Failed to store API key: {result.stderr.strip()}")
            console.print(
                "  [dim]Run manually:[/] pulumi config set --secret pinecone-api-key <key>"
            )
            return False

        console.print("  [green]✓[/] API key stored securely")

        self._print_success(output_dir)
        return True


def select_cloud() -> str:
    console.print()
    console.print(
        Panel.fit(
            f"[bold {BLUE}]Pinecone BYOC Setup Wizard[/]",
            border_style=BLUE,
            padding=(0, 2),
        )
    )
    console.print()
    console.print(
        "  This wizard will set up everything you need to deploy Pinecone BYOC.",
        style="dim",
    )
    console.print()

    console.print(f"  [bold {BLUE}]Select Cloud Provider[/]")
    console.print()
    console.print("  [1] AWS")
    console.print("  [2] GCP")
    console.print("  [3] Azure")
    console.print()

    cloud = _read_input_with_placeholder("Enter choice (1, 2, or 3)", "1")

    if cloud == "1":
        return "aws"
    elif cloud == "2":
        return "gcp"
    elif cloud == "3":
        return "azure"
    else:
        console.print(f"  [red]✗[/] Invalid choice: {cloud}")
        console.print("  [dim]Please choose 1 (AWS), 2 (GCP), or 3 (Azure)[/]")
        sys.exit(1)


def run_setup(
    output_dir: str = ".",
    cloud: str | None = None,
    headless: bool = False,
    stack_name: str = "prod",
    skip_install: bool = False,
    project_name: str | None = None,
    local_package_path: str | None = None,
) -> bool:
    try:
        if not cloud:
            if headless:
                console.print("  [red]✗[/] --cloud is required in headless mode")
                return False
            cloud = select_cloud()

        if cloud == "aws":
            wizard = AWSSetupWizard(
                headless=headless,
                stack_name=stack_name,
                skip_install=skip_install,
                project_name=project_name,
                local_package_path=local_package_path,
            )
            return wizard.run(output_dir)
        elif cloud == "gcp":
            wizard = GCPSetupWizard(
                headless=headless,
                stack_name=stack_name,
                skip_install=skip_install,
                project_name=project_name,
                local_package_path=local_package_path,
            )
            return wizard.run(output_dir)
        elif cloud == "azure":
            wizard = AzureSetupWizard(
                headless=headless,
                stack_name=stack_name,
                skip_install=skip_install,
                project_name=project_name,
                local_package_path=local_package_path,
            )
            return wizard.run(output_dir)
        else:
            console.print(f"  [red]✗[/] Unknown cloud provider: {cloud}")
            console.print("  [dim]Valid options: aws, gcp, azure[/]")
            return False

    except KeyboardInterrupt:
        console.print()
        console.print("  [yellow]Setup cancelled by user[/]")
        return False
    except Exception as e:
        console.print()
        console.print(f"  [red]✗[/] Setup failed: {e}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pinecone BYOC Setup Wizard")
    parser.add_argument("--output-dir", default=".", help="Directory to write project files")
    parser.add_argument(
        "--cloud",
        choices=["aws", "gcp", "azure"],
        help="Cloud provider (aws, gcp, or azure). If not specified, you will be prompted.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without interactive prompts. Reads all inputs from environment variables.",
    )
    parser.add_argument(
        "--stack-name",
        default="prod",
        help="Pulumi stack name (default: prod).",
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help="Pulumi project name. If not specified, you will be prompted.",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip dependency installation and stack initialization.",
    )
    parser.add_argument(
        "--local-package-path",
        default=None,
        help=(
            "Path to a local pulumi-pinecone-nexus-byoc checkout to consume as an "
            "editable dependency via [tool.uv.sources]. If not specified, the "
            "generated project depends on the published PyPI package."
        ),
    )
    args = parser.parse_args()

    success = run_setup(
        args.output_dir,
        args.cloud,
        headless=args.headless,
        stack_name=args.stack_name,
        skip_install=args.skip_install,
        project_name=args.project_name,
        local_package_path=args.local_package_path,
    )
    sys.exit(0 if success else 1)
