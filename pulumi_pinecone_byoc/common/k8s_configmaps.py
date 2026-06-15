"""
k8s configmaps for pinetools helm bootstrapping.
- pc-cluster-information/config: cluster metadata
- pc-pulumi-outputs/config: json blob of pulumi outputs
"""

import json

import pulumi
import pulumi_kubernetes as k8s


class K8sConfigMaps(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        k8s_provider: pulumi.ProviderResource,
        cloud: pulumi.Input[str],
        cell_name: pulumi.Input[str],
        env: pulumi.Input[str],
        is_prod: pulumi.Input[bool],
        domain: pulumi.Input[str],
        region: pulumi.Input[str],
        public_access_enabled: pulumi.Input[bool],
        pulumi_outputs: dict[str, pulumi.Input],
        headless: dict[str, pulumi.Input] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:K8sConfigMaps", name, None, opts)

        cluster_info_ns = k8s.core.v1.Namespace(
            f"{name}-pc-cluster-information-ns",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="pc-cluster-information",
                labels={
                    "name": "pc-cluster-information",
                },
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                delete_before_replace=True,
            ),
        )

        k8s.core.v1.ConfigMap(
            f"{name}-pc-cluster-information-config",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="config",
                namespace="pc-cluster-information",
            ),
            data={
                "cloud": cloud,
                "cell_name": cell_name,
                "env": env,
                "is_prod": pulumi.Output.from_input(is_prod).apply(lambda v: str(v)),
                "domain": domain,
                "region": region,
                "public_access_enabled": pulumi.Output.from_input(public_access_enabled).apply(
                    lambda v: str(v)
                ),
            },
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[cluster_info_ns],
            ),
        )

        pulumi_outputs_ns = k8s.core.v1.Namespace(
            f"{name}-pc-pulumi-outputs-ns",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="pc-pulumi-outputs",
                labels={
                    "name": "pc-pulumi-outputs",
                },
            ),
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                delete_before_replace=True,
            ),
        )

        # Resolve the top-level pulumi outputs and (optionally) the nested
        # headless block together, so a headless block whose fields are
        # Outputs (e.g. a generated index_id) lands as a nested JSON object
        # under the `headless` key. pinetools renders the db helmfile from this
        # ConfigMap merged under `values.config`; the db side gates headless on
        # `config.headless.enabled` and emits PINECONE_HEADLESS__* from the block.
        resolve_inputs: dict[str, pulumi.Input] = {
            k: pulumi.Output.from_input(v) for k, v in pulumi_outputs.items()
        }
        if headless is not None:
            resolve_inputs["__headless__"] = pulumi.Output.all(
                **{k: pulumi.Output.from_input(v) for k, v in headless.items()}
            ).apply(lambda h: {k: v for k, v in dict(h).items() if v is not None})

        def _build_outputs_json(outputs: dict) -> str:
            data = {k: v for k, v in dict(outputs).items() if v is not None}
            headless_block = data.pop("__headless__", None)
            if headless_block:
                data["headless"] = headless_block
            return json.dumps(data)

        outputs_json = pulumi.Output.all(**resolve_inputs).apply(_build_outputs_json)

        k8s.core.v1.ConfigMap(
            f"{name}-pc-pulumi-outputs-config",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name="config",
                namespace="pc-pulumi-outputs",
            ),
            data={
                "pulumi-outputs": outputs_json,
            },
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=k8s_provider,
                depends_on=[pulumi_outputs_ns],
            ),
        )

        self.register_outputs(
            {
                "cluster_info_namespace": cluster_info_ns.metadata.name,
                "pulumi_outputs_namespace": pulumi_outputs_ns.metadata.name,
            }
        )
