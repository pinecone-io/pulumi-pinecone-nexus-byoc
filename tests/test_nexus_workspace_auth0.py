"""Workspace PKCE login: BYOC cells get the prod Auth0 config.

When the workspace flow is enabled (``cpgw_api_url`` set), the deploy-values must
carry ``config.auth0`` (domain/audience/clientId) so nexus-api builds its token
validator and ``/auth/login`` works. Empty values disable the flow, so a BYOC
cell would gate the workspace at the edge but never complete login. BYOC has no
per-tenant Helm overlay (values are built here), so this is the only place the
Auth0 config reaches a BYOC cell.

Run standalone (`python tests/test_nexus_workspace_auth0.py`) or under pytest.
"""

import json
import os
import sys

import pulumi

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pulumi_pinecone_byoc.common.nexus import Nexus  # noqa: E402


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return f"{args.name}-id", args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def _deploy_values(**kwargs):
    """Instantiate Nexus (single FDB mode); return the deploy-values ``data`` Output."""
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
        **kwargs,
    )
    return nexus.deploy_values.data


@pulumi.runtime.test
def test_workspace_flow_sets_prod_auth0_config():
    def check(data):
        app_values = json.loads(data["app-values.yaml"])
        assert app_values["config"]["auth0"] == {
            "domain": "login.pinecone.io",
            "audience": "https://us-central1-production-console.cloudfunctions.net/api/v1",
            "clientId": "tWb4ZIXqJWsWhI0oG5HAhDiCjAjYgVEB",
        }

    return _deploy_values(cpgw_api_url="http://cpgw.internal/internal/cpgw").apply(check)


@pulumi.runtime.test
def test_no_workspace_flow_omits_auth0():
    def check(data):
        app_values = json.loads(data["app-values.yaml"])
        # No cpgw_api_url -> workspace flow off -> no auth0 block (chart default
        # stays empty, which disables login).
        assert "auth0" not in app_values["config"]

    return _deploy_values().apply(check)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
