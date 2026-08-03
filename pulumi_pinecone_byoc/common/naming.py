"""Shared naming conventions and constants for BYOC clusters."""

import re

import pulumi

from .providers import DEFAULT_WORKSPACE_NAME, Environment

ORG_NAME_MAX_LENGTH = 16

# CNAME records created in both DNS and NLB components across all clouds
DNS_CNAMES = ["*.svc", "*.wksp", "metrics", "prometheus"]


def cell_name(environment: Environment) -> pulumi.Output[str]:
    """Derive cell name from environment: e.g. pinecone-byoc-ef7a"""

    def sanitize(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())[:ORG_NAME_MAX_LENGTH]

    return pulumi.Output.all(environment.org_name, environment.env_name).apply(
        lambda args: f"{sanitize(args[0])}-byoc-{args[1].split('.')[0][-4:]}"
    )


def default_workspace_name(
    explicit: str | None, resource_suffix: pulumi.Output[str]
) -> pulumi.Output[str]:
    """Default workspace name is per-cell (`default-<suffix>`) unless explicitly overridden, so a
    leftover from a torn-down cell can't collide project-wide."""
    if explicit:
        return pulumi.Output.from_input(explicit)
    return resource_suffix.apply(lambda s: f"{DEFAULT_WORKSPACE_NAME}-{s}")
