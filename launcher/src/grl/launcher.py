"""GRL launch orchestration."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import yaml

from grl.bundle import PreflightError, verify_bundle
from grl.config import GRLConfig, ResolvedImages
from grl.images import resolve_runtime_images
from grl.k8s import (
    cluster_reachable,
    create_or_update_configmap,
    create_rayjob,
    daemonset_exists,
    helm_upgrade,
    load_kube_client,
    rayjob_manifest,
    read_configmap_value,
    restart_daemonset,
    training_entrypoint,
    wait_for_rollout,
    watch_rayjob,
)
from grl.paths import env_chart_path, state_dir
from grl.terraform import apply_infra, write_env_overlay
from grl.tools import ensure_tools
from grl_proto.environment_client import ListTasksError, list_task_ids

BUNDLE_SYNC_DAEMONSET = "grl-bundle-sync"
ACTIVE_RUN_CONFIGMAP = "grl-active-run"


class GrlError(Exception):
    """Base error for GRL launcher failures."""


class CapacityError(GrlError):
    """Cluster capacity does not match training configuration."""


@dataclass
class LaunchResult:
    run_id: str
    resolved_images: ResolvedImages
    rayjob_name: str | None = None
    task_count: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def validate_capacity(config: GRLConfig) -> list[str]:
    """Pure-config capacity checks. Returns warning messages."""
    warnings: list[str] = []

    rollouts_gpus_per_node = config.rollouts_gpus_per_node()
    training_gpus_per_node = config.training_gpus_per_node()
    tp = config.rollout.tensor_parallel_size
    num_rollout_workers = config.resolved_num_rollout_workers()
    rollout_gpus_total = config.rollout_gpus_total()
    training_gpus_total = config.training_gpus_total()

    if tp > rollouts_gpus_per_node:
        raise CapacityError(
            f"rollout.tensor_parallel_size ({tp}) exceeds rollouts GPUs per node "
            f"({rollouts_gpus_per_node}); each RolloutWorker must fit on one node"
        )
    if rollouts_gpus_per_node % tp != 0:
        raise CapacityError(
            f"rollouts GPUs per node ({rollouts_gpus_per_node}) must be divisible by "
            f"rollout.tensor_parallel_size ({tp}) to avoid stranded GPUs"
        )
    if num_rollout_workers * tp > rollout_gpus_total:
        raise CapacityError(
            f"resolved num_rollout_workers ({num_rollout_workers}) × "
            f"tensor_parallel_size ({tp}) = {num_rollout_workers * tp} exceeds "
            f"rollout GPU capacity ({rollout_gpus_total})"
        )
    if training_gpus_total < 1:
        raise CapacityError(
            f"training GPU capacity is {training_gpus_total}; at least 1 GPU is required"
        )

    max_trajectories = num_rollout_workers * config.rollout.max_concurrent_trajectories
    env_capacity = (
        config.compute.environments.nodes
        * int(config.infra.manager.max_concurrent_envs)
    )
    if max_trajectories > env_capacity:
        warnings.append(
            f"rollout concurrency ({max_trajectories} = {num_rollout_workers} workers × "
            f"{config.rollout.max_concurrent_trajectories} trajectories) may exceed "
            f"environment admission ({env_capacity} = "
            f"{config.compute.environments.nodes} nodes × "
            f"{config.infra.manager.max_concurrent_envs} max_concurrent_envs)"
        )

    print(
        f"Capacity: rollouts {config.compute.rollouts.nodes} nodes × "
        f"{rollouts_gpus_per_node} GPUs → {num_rollout_workers} workers "
        f"(tp={tp}) → ≤{max_trajectories} trajectories | "
        f"training {config.compute.training.nodes} nodes × "
        f"{training_gpus_per_node} GPUs | "
        f"env capacity {env_capacity}"
    )
    for warning in warnings:
        print(f"Warning: {warning}")
    return warnings


def run_preflight(config: GRLConfig, *, dry_run: bool = False) -> None:
    validate_capacity(config)
    if dry_run:
        print("dry-run: skipping AWS identity and bundle checks")
        return
    session = boto3.session.Session()
    identity = session.client("sts").get_caller_identity()
    print(f"AWS identity: {identity.get('Arn', 'unknown')}")
    if config.launch.runs_envs() or config.launch.runs_training():
        verify_bundle(config)


def ensure_managed_tools(config: GRLConfig) -> dict[str, Path]:
    if config.launch.dry_run:
        return {
            "terraform": Path("terraform"),
            "helm": Path("helm"),
            "kubectl": Path("kubectl"),
        }
    return ensure_tools(config.launch.tools)


def persist_run_metadata(
    config: GRLConfig,
    result: LaunchResult,
    api_client=None,
    *,
    dry_run: bool = False,
) -> None:
    metadata = {
        "run_id": result.run_id,
        "config_hash": config.config_hash(),
        "bundle_uri": config.environment.bundle_uri or "",
        "env_id": config.environment.id or "",
        "split": config.environment.split or "",
        "head_image": result.resolved_images.head,
        "rollouts_image": result.resolved_images.rollouts,
        "training_image": result.resolved_images.training,
        "manager_image": result.resolved_images.manager,
        "rayjob_name": result.rayjob_name or "",
    }
    metadata.update(result.metadata)
    local_path = state_dir(result.run_id) / "metadata.json"
    local_path.write_text(json.dumps(metadata, indent=2))
    if api_client is not None:
        create_or_update_configmap(
            api_client,
            name="grl-active-run",
            namespace=config.infra.release_namespace,
            data={key: str(value) for key, value in metadata.items()},
            dry_run=dry_run,
        )


def resolved_kubeconfig(config: GRLConfig) -> Path | None:
    if config.launch.infra.kubeconfig:
        return config.launch.infra.resolved_kubeconfig()
    return None


def load_cluster_client(config: GRLConfig):
    """Connect to the target cluster based on ``launch.cluster_type``.

    BYOK uses the supplied kubeconfig; EKS mints a token for the named cluster.
    """
    if config.launch.is_byok():
        return load_kube_client(kubeconfig=config.launch.infra.resolved_kubeconfig())
    return load_kube_client(
        cluster_name=config.infra.cluster_name,
        region=config.infra.region,
    )


def assert_cluster_present(config: GRLConfig, api_client) -> None:
    """CLUSTER layer check: the target cluster's API is reachable."""
    try:
        cluster_reachable(api_client)
    except Exception as exc:
        raise GrlError(
            f"cluster not reachable ({exc}); deploy the CLUSTER layer first"
        ) from exc


def assert_resources_present(config: GRLConfig, api_client) -> None:
    """RESOURCES layer check: the manager DaemonSet exists."""
    manager = config.resolved_manager()
    if not daemonset_exists(api_client, manager.name, manager.namespace):
        raise GrlError(
            "RESOURCES layer not deployed (manager DaemonSet missing); "
            "run deployment_type=RESOURCES first"
        )


def assert_envs_present(config: GRLConfig) -> None:
    """ENVS layer check: the manager serves a non-empty task catalog."""
    if verify_manager_catalog(config) == 0:
        raise GrlError(
            "ENVS layer not deployed (manager catalog is empty); "
            "run deployment_type=ENVS first"
        )


def assert_layer_present(layer: str, config: GRLConfig, api_client) -> None:
    """Fail unless ``layer`` is already deployed on the target cluster."""
    if layer == "CLUSTER":
        assert_cluster_present(config, api_client)
    elif layer == "RESOURCES":
        assert_resources_present(config, api_client)
    elif layer == "ENVS":
        assert_envs_present(config)


def activate_environment(
    config: GRLConfig,
    tools: dict[str, Path],
    api_client,
    run_id: str,
    *,
    dry_run: bool = False,
) -> None:
    """ENVS layer: apply the launcher-owned ``environments`` chart and roll the
    bundle-sync DaemonSet so the manager hot-reloads the new catalog.

    The manager is never restarted — it is owned by the (untouched) Terraform
    ``resources`` chart and picks up the new bundle via its watcher.
    """
    namespace = config.infra.vm_image_cache.namespace

    # Warn when re-deploying the same bundle (the manager would reload identical
    # content). The currently-deployed URI is recorded in the active-run CM.
    if not dry_run and api_client is not None and config.environment.bundle_uri:
        deployed = read_configmap_value(
            api_client, ACTIVE_RUN_CONFIGMAP, config.infra.release_namespace, "bundle_uri"
        )
        if deployed and deployed == config.environment.bundle_uri:
            print(
                f"warning: environment bundle unchanged ({deployed}); "
                "re-syncing the same S3 URI"
            )

    overlay_path = write_env_overlay(config, run_id)
    helm_upgrade(
        tools["helm"],
        config.infra.env_release_name,
        env_chart_path(),
        namespace,
        [overlay_path],
        kubeconfig=resolved_kubeconfig(config),
        dry_run=dry_run,
    )

    # Roll the bundle-sync DaemonSet so its initContainer re-syncs and rewrites
    # the .ready sentinel; the manager reloads its catalog with no pod restart.
    restart_daemonset(api_client, BUNDLE_SYNC_DAEMONSET, namespace, dry_run=dry_run)
    wait_for_rollout(api_client, BUNDLE_SYNC_DAEMONSET, namespace, dry_run=dry_run)


def verify_manager_catalog(config: GRLConfig) -> int:
    addr = config.environment.server_addr
    split = config.environment.split
    timeout = config.environment.rpc_timeouts.list_tasks_secs
    try:
        task_ids = asyncio.run(
            list_task_ids(addr=addr, split=split, timeout_secs=timeout)
        )
    except ListTasksError as exc:
        raise PreflightError(str(exc)) from exc
    return len(task_ids)


def wait_for_manager_catalog(
    config: GRLConfig,
    *,
    timeout_secs: float = 300.0,
    poll_interval_secs: float = 5.0,
) -> int:
    """Poll the manager until it serves a non-empty catalog (bundle-sync is
    asynchronous), returning the observed task count. Returns the last count
    seen if the timeout elapses first."""
    deadline = time.monotonic() + timeout_secs
    count = 0
    while time.monotonic() < deadline:
        count = verify_manager_catalog(config)
        if count > 0:
            return count
        time.sleep(poll_interval_secs)
    return count


def submit_training_job(
    config: GRLConfig,
    run_id: str,
    api_client,
    *,
    dry_run: bool = False,
) -> str:
    configmap_name = f"grl-run-{run_id}"
    training_yaml = config.training_yaml(run_id=run_id)
    create_or_update_configmap(
        api_client,
        name=configmap_name,
        namespace=config.infra.ray_cluster.namespace,
        data={"config.yaml": training_yaml},
        dry_run=dry_run,
    )
    entrypoint = training_entrypoint(training_yaml)
    rayjob_name = f"grl-run-{run_id}"
    manifest = rayjob_manifest(
        name=rayjob_name,
        namespace=config.infra.ray_cluster.namespace,
        ray_cluster_name=config.infra.ray_cluster.name,
        entrypoint=entrypoint,
    )
    create_rayjob(api_client, manifest, dry_run=dry_run)
    if config.launch.job.wait and not dry_run:
        status = watch_rayjob(
            api_client,
            rayjob_name,
            config.infra.ray_cluster.namespace,
        )
        if status != "SUCCEEDED":
            raise GrlError(f"RayJob {rayjob_name} finished with status {status}")
    return rayjob_name


def launch(config: GRLConfig, *, config_path: Path | None = None) -> LaunchResult:
    dry_run = config.launch.dry_run
    launch_cfg = config.launch
    run_id = config.resolve_run_id()
    if config.telemetry.run_id is None:
        config.telemetry.run_id = run_id
    print(
        f"GRL launch run_id={run_id} "
        f"deployment_type={launch_cfg.deployment_type} "
        f"cluster_type={launch_cfg.cluster_type}"
    )

    tools = ensure_managed_tools(config)
    resolved = resolve_runtime_images(config, dry_run=dry_run)
    config.apply_resolved_images(resolved)
    print(f"Resolved images: head={resolved.head} manager={resolved.manager}")

    run_preflight(config, dry_run=dry_run)
    if launch_cfg.preflight_only:
        print("Preflight complete.")
        return LaunchResult(run_id=run_id, resolved_images=resolved)

    api_client = None

    # Fail fast if a single-layer run's prerequisite layer isn't already present.
    prev = launch_cfg.required_present_layer()
    if prev is not None and not dry_run:
        if prev != "ENVS":
            api_client = load_cluster_client(config)
        assert_layer_present(prev, config, api_client)

    # --- CLUSTER layer ---
    if launch_cfg.runs_cluster():
        if launch_cfg.is_eks():
            # For EKS+FULL this single apply also deploys RESOURCES
            # (deploy_workloads=True), so the RESOURCES step below is a no-op.
            apply_infra(config, resolved, tools["terraform"], run_id, dry_run=dry_run)
        elif dry_run:
            print("dry-run: verify BYOK cluster reachable")
        else:
            api_client = api_client or load_cluster_client(config)
            assert_cluster_present(config, api_client)

    # --- RESOURCES layer ---
    if launch_cfg.runs_resources() and (launch_cfg.is_byok() or not launch_cfg.runs_cluster()):
        apply_infra(config, resolved, tools["terraform"], run_id, dry_run=dry_run)

    if (launch_cfg.runs_envs() or launch_cfg.runs_training()) and not dry_run:
        api_client = api_client or load_cluster_client(config)

    # --- ENVS layer ---
    task_count: int | None = None
    if launch_cfg.runs_envs():
        activate_environment(config, tools, api_client, run_id, dry_run=dry_run)
        if launch_cfg.environment.verify and not dry_run:
            task_count = wait_for_manager_catalog(config)
            print(f"Manager catalog: {task_count} tasks")

    # --- TRAINING layer ---
    rayjob_name: str | None = None
    if launch_cfg.runs_training():
        rayjob_name = submit_training_job(config, run_id, api_client, dry_run=dry_run)
        print(f"Submitted RayJob {rayjob_name}")

    result = LaunchResult(
        run_id=run_id,
        resolved_images=resolved,
        rayjob_name=rayjob_name,
        task_count=task_count,
    )
    persist_run_metadata(config, result, api_client, dry_run=dry_run)
    print("Launch complete.")
    return result


def write_init_config(destination: Path) -> None:
    example = Path(__file__).resolve().parents[2] / "example-config.yaml"
    if example.is_file():
        destination.write_text(example.read_text())
        return
    destination.write_text(yaml.safe_dump({"model": "Qwen/Qwen3.5-4B"}, sort_keys=False))
