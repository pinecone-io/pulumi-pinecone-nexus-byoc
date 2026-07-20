"""Nexus uninstaller - `helm uninstall`s the Nexus releases on destroy.

With the image+Job delivery model there is no Pulumi ``Release`` whose deletion
would uninstall the charts, so an explicit teardown step runs `helm uninstall`
from inside the cluster (the deploy image carries helm; the ``nexus-deploy`` SA
carries the RBAC + ``regcred``). On a full-stack destroy the cluster deletion
would remove the workloads anyway; this matters for disabling Nexus while keeping
the cluster, and for not leaving orphaned helm release state behind.
"""

import contextlib
import json
import time
import uuid
from typing import Any

import pulumi
from pulumi.dynamic import (
    CreateResult,
    DiffResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

# Reverse of install order (app before fdb). --ignore-not-found keeps it
# idempotent: a missing release is a no-op, real failures still surface.
_UNINSTALL_SCRIPT = (
    "helm uninstall nexus --namespace nexus --ignore-not-found --wait --timeout 5m && "
    "helm uninstall nexus-fdb --namespace nexus --ignore-not-found --wait --timeout 5m"
)


class NexusUninstallerProvider(ResourceProvider):
    def create(self, props: dict[str, Any]) -> CreateResult:
        return CreateResult(id_="nexus-uninstaller-ready", outs=props)

    def diff(self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]) -> DiffResult:
        changed = (
            _olds.get("kubeconfig") != _news.get("kubeconfig")
            or _olds.get("deploy_image") != _news.get("deploy_image")
            or _olds.get("cloud") != _news.get("cloud")
        )
        return DiffResult(changes=changed)

    def update(self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]) -> UpdateResult:
        return UpdateResult(outs=_news)

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        import yaml
        from kubernetes import client, config
        from kubernetes.client.rest import ApiException

        kubeconfig_str = _props.get("kubeconfig")
        if not kubeconfig_str:
            raise Exception("kubeconfig not provided to nexus uninstaller")

        deploy_image = _props.get("deploy_image")
        if not deploy_image:
            raise Exception("deploy_image not provided to nexus uninstaller")

        try:
            kubeconfig = json.loads(kubeconfig_str)
        except (json.JSONDecodeError, ValueError):
            try:
                kubeconfig = yaml.safe_load(kubeconfig_str)
            except yaml.YAMLError as e:
                raise Exception(f"Failed to parse kubeconfig as JSON or YAML: {e}") from e

        # GKE exec-based auth needs a fresh gcloud token in the dynamic provider
        # context (no kubectl/gcloud credential plumbing at destroy time).
        if _props.get("cloud") == "gcp":
            try:
                import subprocess

                token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"],
                    text=True,
                    timeout=10,
                ).strip()
                for user in kubeconfig.get("users", []):
                    user["user"] = {"token": token}
            except Exception as e:
                pulumi.log.warn(f"Failed to get gcloud token: {e}")

        # EKS mirror of the GKE branch: the kubeconfig authenticates via an exec
        # plugin (`aws eks get-token`), which the python client won't run in the
        # dynamic-provider context. Run the kubeconfig's own exec spec here and
        # swap the user entry for the minted bearer token.
        if _props.get("cloud") == "aws":
            try:
                import os
                import subprocess

                for user in kubeconfig.get("users", []):
                    exec_spec = (user.get("user") or {}).get("exec")
                    if not exec_spec:
                        continue
                    env = os.environ.copy()
                    for pair in exec_spec.get("env") or []:
                        env[pair["name"]] = pair["value"]
                    cred = json.loads(
                        subprocess.check_output(
                            [exec_spec["command"], *(exec_spec.get("args") or [])],
                            text=True,
                            timeout=30,
                            env=env,
                        )
                    )
                    user["user"] = {"token": cred["status"]["token"]}
            except Exception as e:
                pulumi.log.warn(f"Failed to get EKS token: {e}")

        config.load_kube_config_from_dict(kubeconfig)

        batch_v1 = client.BatchV1Api()
        core_v1 = client.CoreV1Api()

        namespace = "nexus"
        job_name = f"nexus-uninstall-{uuid.uuid4().hex[:8]}"

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name, namespace=namespace),
            spec=client.V1JobSpec(
                backoff_limit=1,
                active_deadline_seconds=900,
                ttl_seconds_after_finished=300,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        service_account_name="nexus-deploy",
                        restart_policy="OnFailure",
                        containers=[
                            client.V1Container(
                                name="uninstall",
                                image=deploy_image,
                                command=["/bin/sh", "-c"],
                                args=[_UNINSTALL_SCRIPT],
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "ephemeral-storage": "1Gi",
                                        "memory": "256Mi",
                                        "cpu": "100m",
                                    },
                                    limits={"ephemeral-storage": "2Gi", "memory": "1Gi"},
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        pulumi.log.info(f"Creating nexus uninstall job: {job_name}")
        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except ApiException as e:
            if e.status == 404:
                # The nexus namespace is already gone — nothing to uninstall.
                pulumi.log.warn(f"nexus namespace not found, skipping uninstall: {e}")
                return
            if e.status == 409:
                pulumi.log.warn(f"Uninstall job {job_name} already exists, waiting for it")
            else:
                raise Exception(f"Failed to create nexus uninstall job: {e}") from e

        timeout_seconds = 900
        poll_interval = 10
        elapsed = 0
        while elapsed < timeout_seconds:
            try:
                status = batch_v1.read_namespaced_job_status(
                    name=job_name, namespace=namespace
                ).status
                if status.succeeded and status.succeeded > 0:
                    pulumi.log.info(f"Nexus uninstall job {job_name} completed")
                    return
                if status.failed and status.failed > 0:
                    logs = ""
                    pods = core_v1.list_namespaced_pod(
                        namespace=namespace, label_selector=f"job-name={job_name}"
                    )
                    for pod in pods.items:
                        with contextlib.suppress(Exception):
                            logs += core_v1.read_namespaced_pod_log(
                                name=pod.metadata.name, namespace=namespace
                            )
                    raise Exception(
                        f"Nexus uninstall job {job_name} failed. "
                        f"Run 'pulumi destroy' again to retry.\nLogs:\n{logs}"
                    )
            except ApiException as e:
                if e.status == 404:
                    pulumi.log.warn(f"Job {job_name} not found, may have been deleted")
                    return
                raise
            time.sleep(poll_interval)
            elapsed += poll_interval

        raise Exception(
            f"Nexus uninstall job {job_name} timed out after {timeout_seconds}s. "
            f"Run 'pulumi destroy' again to retry."
        )


class NexusUninstaller(Resource):
    """Runs `helm uninstall` for the Nexus releases on destroy.

    Must depend on the ``Nexus`` component so its ``delete()`` runs while the
    cluster, the ``nexus-deploy`` SA, and ``regcred`` still exist.
    """

    kubeconfig: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        kubeconfig: pulumi.Input[str],
        deploy_image: pulumi.Input[str],
        cloud: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ):
        props = {
            "kubeconfig": kubeconfig,
            "deploy_image": deploy_image,
            "cloud": cloud,
        }
        super().__init__(NexusUninstallerProvider(), name, props, opts)
