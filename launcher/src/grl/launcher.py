"""GRL launch orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import yaml

from grl.bundle import verify_bundle
from grl.config import GRLConfig, ResolvedImages
from grl.errors import GrlError
from grl.images import resolve_runtime_images
from grl.k8s import (
    create_or_update_configmap,
    create_rayjob,
    helm_upgrade,
    load_kube_client,
    rayjob_manifest,
    restart_daemonset,
    training_entrypoint,
    wait_for_rollout,
    watch_rayjob,
)
from grl.paths import helm_chart_path, state_dir
from grl.terraform import apply_infra, write_helm_overlay
from grl.tools import ensure_tools
from grl.verify import verify_manager_catalog


@dataclass
class LaunchResult:
    run_id: str
    resolved_images: ResolvedImages
    rayjob_name: str | None = None
    task_count: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def run_preflight(config: GRLConfig, *, dry_run: bool = False) -> None:
    if dry_run:
        print("dry-run: preflight checks")
        return
    session = boto3.session.Session()
    identity = session.client("sts").get_caller_identity()
    print(f"AWS identity: {identity.get('Arn', 'unknown')}")
    if config.launch.environment.activate or config.launch.job.submit:
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


def activate_environment(
    config: GRLConfig,
    tools: dict[str, Path],
    api_client,
    run_id: str,
    *,
    dry_run: bool = False,
) -> None:
    overlay_path = write_helm_overlay(config, run_id)
    chart = helm_chart_path()
    base_values = chart / "values.yaml"
    values_files = [base_values, overlay_path]
    helm_upgrade(
        tools["helm"],
        config.infra.release_name,
        chart,
        config.infra.release_namespace,
        values_files,
        dry_run=dry_run,
    )

    refresh = config.launch.environment.refresh_vm_cache
    manager = config.resolved_manager()
    restart_manager = True
    restart_vm_cache = refresh == "always"
    if refresh == "auto" and config.infra.vm_image_cache.bucket:
        restart_vm_cache = True

    if restart_vm_cache:
        restart_daemonset(
            api_client,
            "vm-image-cache",
            config.infra.vm_image_cache.namespace,
            dry_run=dry_run,
        )
        wait_for_rollout(
            api_client,
            "vm-image-cache",
            config.infra.vm_image_cache.namespace,
            dry_run=dry_run,
        )

    if restart_manager:
        restart_daemonset(
            api_client,
            manager.name,
            manager.namespace,
            dry_run=dry_run,
        )
        wait_for_rollout(
            api_client,
            manager.name,
            manager.namespace,
            dry_run=dry_run,
        )


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
    run_id = config.resolve_run_id()
    if config.telemetry.run_id is None:
        config.telemetry.run_id = run_id
    print(f"GRL launch run_id={run_id}")

    tools = ensure_managed_tools(config)
    resolved = resolve_runtime_images(config, dry_run=dry_run)
    config.apply_resolved_images(resolved)
    print(f"Resolved images: head={resolved.head} manager={resolved.manager}")

    run_preflight(config, dry_run=dry_run)
    if config.launch.preflight_only:
        print("Preflight complete.")
        return LaunchResult(run_id=run_id, resolved_images=resolved)

    if config.launch.infra.apply:
        if "terraform" not in tools:
            tools = ensure_managed_tools(config)
        apply_infra(
            config,
            resolved,
            tools["terraform"],
            run_id,
            dry_run=dry_run,
        )

    api_client = None
    if config.launch.environment.activate or config.launch.job.submit:
        if not dry_run:
            if config.launch.infra.auto_kubeconfig or config.launch.infra.apply:
                api_client = load_kube_client(
                    cluster_name=config.infra.cluster_name,
                    region=config.infra.region,
                )
            else:
                api_client = load_kube_client()

    task_count: int | None = None
    if config.launch.environment.activate:
        if "helm" not in tools:
            tools = ensure_managed_tools(config)
        activate_environment(config, tools, api_client, run_id, dry_run=dry_run)
        if config.launch.environment.verify and not dry_run:
            task_count = verify_manager_catalog(config)
            print(f"Manager catalog verified: {task_count} tasks")

    rayjob_name: str | None = None
    if config.launch.job.submit:
        rayjob_name = submit_training_job(
            config,
            run_id,
            api_client,
            dry_run=dry_run,
        )
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
