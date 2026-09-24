"""Nexus rbac, access, oidc and workspaces chart values.

Covers:
- each key renders under `config` in app-values only when set, so a cell that
  names none of them is byte-identical to one deployed before these fields
  existed;
- `workspaces_enabled=False` drops `workspacesEnabled`, `gateway.workspaceAuth`
  and the Auth0 block while leaving the CPGW index client wired;
- `NexusConfig` refuses, at preview time, the combinations Nexus does not
  support, plus a partial `[oidc]` and an unknown mode.

Run standalone (`python tests/test_nexus_rbac_config.py`) or under pytest.
"""

import json
import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus, NexusConfig  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _app_values(**kwargs):
    """Instantiate Nexus with CPGW wired; return the deploy-values ``data`` Output."""
    # Set within the runtime test (event loop active) rather than at import time.
    pulumi.runtime.set_mocks(_Mocks(), preview=False)
    provider = pulumi.ProviderResource("pulumi:providers:kubernetes", "k8s", {})
    nexus = Nexus(
        "t",
        k8s_provider=provider,
        image_registry="reg.example/nexus",
        nexus_version="1.2.3",
        byoc_env="e.byoc",
        cloud="gcp",
        region="us-central1",
        pinecone_prod=True,
        byoc_project_id="proj",
        cpgw_api_url="https://api.example/internal/cpgw",
        **kwargs,
    )
    return nexus.deploy_values.data


def _expect_value_error(expected: str, **kwargs):
    """Assert NexusConfig refuses, and that it refused for `expected`.

    The gates run in order and an earlier one shadows a later one, so matching on
    the text is what keeps each case pinned to the gate it is named for.
    """
    try:
        NexusConfig(**kwargs)
    except ValueError as e:
        assert expected in str(e), f"refused, but not for {expected!r}: {e}"
        return
    raise AssertionError(f"expected ValueError mentioning {expected!r}")


@pulumi.runtime.test
def test_unset_keys_are_omitted_entirely():
    def check(data):
        cfg = json.loads(data["app-values.yaml"])["config"]
        assert "rbac" not in cfg
        assert "access" not in cfg
        assert "oidc" not in cfg

    return _app_values().apply(check)


@pulumi.runtime.test
def test_rbac_mode_renders_under_config():
    def check(data):
        cfg = json.loads(data["app-values.yaml"])["config"]
        assert cfg["rbac"] == {"mode": "shadow"}

    return _app_values(rbac_mode="shadow").apply(check)


@pulumi.runtime.test
def test_workspaces_on_keeps_workspace_wiring_and_auth0():
    def check(data):
        values = json.loads(data["app-values.yaml"])
        assert values["config"]["workspacesEnabled"] is True
        assert values["gateway"]["workspaceAuth"] is True
        assert values["config"]["auth0"]["domain"]

    return _app_values().apply(check)


@pulumi.runtime.test
def test_workspaces_off_drops_workspace_wiring_but_keeps_cpgw():
    def check(data):
        values = json.loads(data["app-values.yaml"])
        assert "workspacesEnabled" not in values["config"]
        assert "auth0" not in values["config"]
        assert "workspaceAuth" not in values["gateway"]
        # CPGW is the index client, not part of the workspace lifecycle: Nexus
        # accepts a fully-set CPGW triple with workspaces off, and dropping it
        # here would move index create back to the managed public API.
        assert values["config"]["cpgwApiUrl"] == "https://api.example/internal/cpgw"
        assert values["config"]["byocDocsApiUrl"]

    return _app_values(workspaces_enabled=False).apply(check)


@pulumi.runtime.test
def test_bootstrap_admins_and_oidc_render_on_a_workspaceless_cell():
    def check(data):
        cfg = json.loads(data["app-values.yaml"])["config"]
        assert cfg["access"] == {"bootstrapAdmins": "a@x.com,b@x.com"}
        assert cfg["oidc"] == {
            "issuer": "https://t.okta.com/oauth2/default",
            "audience": "api://nexus",
        }

    return _app_values(
        workspaces_enabled=False,
        bootstrap_admins="a@x.com,b@x.com",
        oidc_issuer="https://t.okta.com/oauth2/default",
        oidc_audience="api://nexus",
    ).apply(check)


@pulumi.runtime.test
def test_bootstrap_admins_accepts_a_list():
    def check(data):
        cfg = json.loads(data["app-values.yaml"])["config"]
        assert cfg["access"] == {"bootstrapAdmins": ["a@x.com", "b@x.com"]}

    return _app_values(
        workspaces_enabled=False,
        bootstrap_admins=["a@x.com", "b@x.com"],
        oidc_issuer="https://t.okta.com/oauth2/default",
        oidc_audience="api://nexus",
    ).apply(check)


def test_config_accepts_every_rbac_mode():
    for mode in ("", "off", "shadow", "enforce"):
        assert NexusConfig(rbac_mode=mode).rbac_mode == mode


def test_config_rejects_unknown_rbac_mode():
    _expect_value_error("rbac_mode must be", rbac_mode="enforced")


def test_config_rejects_bootstrap_admins_with_workspaces():
    # No oidc here on purpose: with an issuer set, the oidc check runs first and
    # this case would pass without ever reaching the one it is named for.
    _expect_value_error(
        "bootstrap_admins is not supported alongside workspaces",
        bootstrap_admins="a@x.com",
    )


def test_config_rejects_bootstrap_admins_without_an_issuer():
    _expect_value_error(
        "bootstrap_admins requires oidc_issuer",
        workspaces_enabled=False,
        bootstrap_admins="a@x.com",
    )


def test_config_rejects_oidc_alongside_workspaces():
    _expect_value_error(
        "oidc_issuer is not supported alongside workspaces",
        oidc_issuer="https://t.okta.com/oauth2/default",
        oidc_audience="api://nexus",
    )


def test_config_rejects_partial_oidc():
    _expect_value_error(
        "must be set together",
        workspaces_enabled=False,
        oidc_issuer="https://t.okta.com/oauth2/default",
    )
    _expect_value_error(
        "must be set together",
        workspaces_enabled=False,
        oidc_audience="api://nexus",
    )


def test_config_default_is_unchanged():
    cfg = NexusConfig()
    assert cfg.rbac_mode == ""
    assert cfg.bootstrap_admins is None
    assert cfg.oidc_issuer is None
    assert cfg.oidc_audience is None
    assert cfg.workspaces_enabled is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
