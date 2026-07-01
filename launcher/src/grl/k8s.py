"""Kubernetes and Helm helpers."""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.signers import RequestSigner
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from grl.errors import KubernetesError
from grl.tools import run_tool


def generate_eks_token(cluster_name: str, region: str) -> str:
    session = boto3.session.Session()
    sts_client = session.client("sts", region_name=region)
    service_id = sts_client.meta.service_model.service_id
    signer = RequestSigner(
        service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )
    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    presigned_url = signer.generate_presigned_url(
        params,
        region_name=region,
        expires_in=60,
        operation_name="",
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(presigned_url.encode()).decode().rstrip("=")
    return token


def configure_eks_client(cluster_name: str, region: str) -> client.ApiClient:
    eks = boto3.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    token = generate_eks_token(cluster_name, region)
    ca_data = base64.b64decode(cluster["certificateAuthority"]["data"])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as ca_file:
        ca_file.write(ca_data)
        ca_path = ca_file.name
    configuration = client.Configuration()
    configuration.host = cluster["endpoint"]
    configuration.ssl_ca_cert = ca_path
    configuration.api_key = {"authorization": f"Bearer {token}"}
    return client.ApiClient(configuration)


def load_kube_client(
    *,
    cluster_name: str | None = None,
    region: str | None = None,
) -> client.ApiClient:
    if cluster_name and region:
        return configure_eks_client(cluster_name, region)
    try:
        config.load_kube_config()
        return client.ApiClient()
    except config.ConfigException as exc:
        raise KubernetesError(
            "no kubeconfig found; set launch.infra.apply or configure kubectl context"
        ) from exc


def helm_upgrade(
    helm_bin: Path,
    release_name: str,
    chart_path: Path,
    namespace: str,
    values_files: list[Path],
    *,
    dry_run: bool = False,
) -> None:
    args = [
        "upgrade",
        "--install",
        release_name,
        str(chart_path),
        "--namespace",
        namespace,
        "--create-namespace",
    ]
    for values_file in values_files:
        args.extend(["-f", str(values_file)])
    if dry_run:
        args.append("--dry-run")
    try:
        run_tool(helm_bin, args, dry_run=dry_run)
    except Exception as exc:
        raise KubernetesError(f"helm upgrade failed: {exc}") from exc


def create_or_update_configmap(
    api_client: client.ApiClient,
    name: str,
    namespace: str,
    data: dict[str, str],
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"dry-run: create/update ConfigMap {namespace}/{name}")
        return
    core = client.CoreV1Api(api_client)
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data=data,
    )
    try:
        core.replace_namespaced_config_map(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 404:
            raise KubernetesError(f"ConfigMap replace failed: {exc}") from exc
        core.create_namespaced_config_map(namespace=namespace, body=body)


def restart_daemonset(
    api_client: client.ApiClient,
    name: str,
    namespace: str,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"dry-run: restart DaemonSet {namespace}/{name}")
        return
    apps = client.AppsV1Api(api_client)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "grl.io/restartedAt": now,
                    }
                }
            }
        }
    }
    try:
        apps.patch_namespaced_daemon_set(name=name, namespace=namespace, body=body)
    except ApiException as exc:
        raise KubernetesError(f"DaemonSet restart failed for {name}: {exc}") from exc


def wait_for_rollout(
    api_client: client.ApiClient,
    name: str,
    namespace: str,
    *,
    timeout_secs: float = 600.0,
    poll_interval_secs: float = 5.0,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"dry-run: wait for DaemonSet rollout {namespace}/{name}")
        return
    apps = client.AppsV1Api(api_client)
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        ds = apps.read_namespaced_daemon_set(name=name, namespace=namespace)
        status = ds.status
        desired = status.desired_number_scheduled or 0
        ready = status.number_ready or 0
        updated = status.updated_number_scheduled or 0
        if desired > 0 and ready == desired and updated == desired:
            return
        time.sleep(poll_interval_secs)
    raise KubernetesError(f"timed out waiting for DaemonSet {namespace}/{name} rollout")


def training_entrypoint(training_yaml: str) -> str:
    """Build a RayJob entrypoint that materializes inline training config."""
    encoded = base64.b64encode(training_yaml.encode()).decode()
    return (
        "python -c \"import base64,tempfile,subprocess,sys; "
        f"d=base64.b64decode('{encoded}'); "
        "p=tempfile.NamedTemporaryFile('wb',suffix='.yaml',delete=False); "
        "p.write(d); p.close(); "
        "sys.exit(subprocess.call(['python','-m','training.main','--config',p.name]))\""
    )


def rayjob_manifest(
    *,
    name: str,
    namespace: str,
    ray_cluster_name: str,
    entrypoint: str,
    shutdown_after_job_finishes: bool = False,
) -> dict[str, Any]:
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "entrypoint": entrypoint,
            "rayClusterName": ray_cluster_name,
            "shutdownAfterJobFinishes": shutdown_after_job_finishes,
            "submitMode": "K8sJobMode",
            "metadata": {
                "labels": {"app.kubernetes.io/managed-by": "grl"},
            },
        },
    }


def create_rayjob(
    api_client: client.ApiClient,
    manifest: dict[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print("dry-run: create RayJob")
        print(yaml.safe_dump(manifest, sort_keys=False))
        return
    custom = client.CustomObjectsApi(api_client)
    group = "ray.io"
    version = "v1"
    plural = "rayjobs"
    namespace = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    try:
        custom.create_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            body=manifest,
        )
    except ApiException as exc:
        if exc.status != 409:
            raise KubernetesError(f"RayJob create failed: {exc}") from exc
        custom.replace_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            name=name,
            body=manifest,
        )


def watch_rayjob(
    api_client: client.ApiClient,
    name: str,
    namespace: str,
    *,
    timeout_secs: float = 3600.0,
    poll_interval_secs: float = 10.0,
    dry_run: bool = False,
) -> str:
    if dry_run:
        print(f"dry-run: watch RayJob {namespace}/{name}")
        return "dry-run"
    custom = client.CustomObjectsApi(api_client)
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        resource = custom.get_namespaced_custom_object(
            group="ray.io",
            version="v1",
            namespace=namespace,
            plural="rayjobs",
            name=name,
        )
        status = resource.get("status", {})
        job_status = status.get("jobStatus", "")
        if job_status in {"SUCCEEDED", "FAILED", "STOPPED"}:
            return job_status
        time.sleep(poll_interval_secs)
    raise KubernetesError(f"timed out waiting for RayJob {namespace}/{name}")
