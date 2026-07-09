"""Unified launch configuration for GRL training and cluster infra."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from grl_config.infra import ComputeConfig
from grl_config.providers import provider_for_cluster_type
from grl_config.run_id import new_run_id
from grl_config.training import DEFAULT_CONFIG_PATH, GRLConfig as TrainingGRLConfig
from grl.paths import validate_cluster_name
from pydantic import BaseModel, Field, field_validator, model_validator


# Ordered deployment layers, each building on the one below. A single-layer
# deployment_type deploys exactly its layer and requires the layer beneath it to
# already be present; FULL runs all four in order.
LAYER_ORDER = ("CLUSTER", "RESOURCES", "ENVS", "TRAINING")
DEPLOYMENT_TYPES = (*LAYER_ORDER, "FULL")
CLUSTER_TYPES = ("EKS", "BYOK")
DeploymentType = Literal["CLUSTER", "RESOURCES", "ENVS", "TRAINING", "FULL"]
ClusterType = Literal["EKS", "BYOK"]


class LaunchToolsConfig(BaseModel):
    auto_install: bool = True
    terraform: Literal["terraform", "tofu"] = "terraform"
    terraform_version: str = "1.13.0"
    helm_version: str = "3.16.4"
    kubectl_version: str = "1.32.0"


class LaunchInfraConfig(BaseModel):
    terraform_dir: str = "infra/aws"
    byok_terraform_dir: str = "infra/byok"
    kubeconfig: str | None = None
    auto_kubeconfig: bool = True

    def uses_kubeconfig(self) -> bool:
        return self.kubeconfig is not None

    def resolved_kubeconfig(self) -> Path:
        if not self.kubeconfig:
            raise ValueError("launch.infra.kubeconfig is not set")
        path = Path(self.kubeconfig).expanduser()
        if not path.is_file():
            raise ValueError(f"kubeconfig not found: {path}")
        return path


class LaunchEnvironmentConfig(BaseModel):
    verify: bool = True
    refresh_vm_cache: Literal["auto", "always", "never"] = "auto"
    purge_cache: bool = False


class LaunchJobConfig(BaseModel):
    force: bool = False
    wait: bool = False
    backend: Literal["rayjob"] = "rayjob"


class LaunchConfig(BaseModel):
    dry_run: bool = False
    preflight_only: bool = False
    deployment_type: DeploymentType = "FULL"
    cluster_type: ClusterType = "EKS"
    tools: LaunchToolsConfig = Field(default_factory=LaunchToolsConfig)
    infra: LaunchInfraConfig = Field(default_factory=LaunchInfraConfig)
    environment: LaunchEnvironmentConfig = Field(default_factory=LaunchEnvironmentConfig)
    job: LaunchJobConfig = Field(default_factory=LaunchJobConfig)

    @field_validator("deployment_type", mode="before")
    @classmethod
    def _validate_deployment_type(cls, value: object) -> object:
        if value not in DEPLOYMENT_TYPES:
            raise ValueError(
                f"invalid deployment_type {value!r}; "
                f"valid options: {', '.join(DEPLOYMENT_TYPES)}"
            )
        return value

    @field_validator("cluster_type", mode="before")
    @classmethod
    def _validate_cluster_type(cls, value: object) -> object:
        if value not in CLUSTER_TYPES:
            raise ValueError(
                f"invalid cluster_type {value!r}; "
                f"valid options: {', '.join(CLUSTER_TYPES)}"
            )
        return value

    def layers(self) -> tuple[str, ...]:
        """Layers this run touches: all four for FULL, else the single mode."""
        if self.deployment_type == "FULL":
            return LAYER_ORDER
        return (self.deployment_type,)

    def runs_cluster(self) -> bool:
        return "CLUSTER" in self.layers()

    def runs_resources(self) -> bool:
        return "RESOURCES" in self.layers()

    def runs_envs(self) -> bool:
        return "ENVS" in self.layers()

    def runs_training(self) -> bool:
        return "TRAINING" in self.layers()

    def is_eks(self) -> bool:
        return self.cluster_type == "EKS"

    def is_byok(self) -> bool:
        return self.cluster_type == "BYOK"

    def required_present_layer(self) -> str | None:
        """The layer that must already be deployed before this run's first layer.

        None for FULL (it deploys every layer itself, bottom-up).
        """
        if self.deployment_type == "FULL":
            return None
        idx = LAYER_ORDER.index(self.deployment_type)
        return LAYER_ORDER[idx - 1] if idx > 0 else None


# --- Runtime images ---


class TrainingImagesConfig(BaseModel):
    head: str = "auto"
    rollouts: str = "auto"
    training: str = "auto"


class ImageRegistryConfig(BaseModel):
    type: Literal["ghcr", "ecr"] = "ghcr"
    create: bool = False
    repository_prefix: str = "grl"


class ImageBuildConfig(BaseModel):
    source: Literal["checkout", "git", "path"] = "checkout"
    context_dir: str = "."
    docker: str = "auto"
    platforms: list[str] = Field(default_factory=lambda: ["linux/amd64"])
    path: str | None = None


class ImagesConfig(BaseModel):
    mode: Literal["published", "custom", "build_and_push"] = "published"
    registry: str = "ghcr.io/salazar-99/grl"
    tag: str = "0.1.0"
    training: TrainingImagesConfig = Field(default_factory=TrainingImagesConfig)
    manager: str = "auto"
    push_registry: ImageRegistryConfig = Field(default_factory=ImageRegistryConfig)
    build: ImageBuildConfig = Field(default_factory=ImageBuildConfig)


class ResolvedImages(BaseModel):
    head: str
    rollouts: str
    training: str
    manager: str


# --- Infra (matches infra/modules/resources/chart/values.yaml + Terraform vars) ---


class RayClusterImagesConfig(BaseModel):
    head: str = "grl-training:head"
    rollouts: str = "grl-training:rollouts"
    training: str = "grl-training:training"


class RayClusterConfig(BaseModel):
    name: str = "grl-ray"
    namespace: str = "default"
    version: str = "2.55.1"
    images: RayClusterImagesConfig = Field(default_factory=RayClusterImagesConfig)


class OtelCollectorUpstreamConfig(BaseModel):
    endpoint: str = "https://otel.gerardosalazar.com"
    username: str = ""
    password: str = ""


class OtelCollectorConfig(BaseModel):
    name: str = "grl-collector"
    namespace: str = "default"
    upstream: OtelCollectorUpstreamConfig = Field(
        default_factory=OtelCollectorUpstreamConfig
    )


class ManagerConfig(BaseModel):
    name: str = "grl-manager"
    namespace: str = "default"
    image: str = "grl-manager:latest"
    port: int = 50051
    bundle_uri: str = Field(default="", alias="bundleUri")
    env_id: str = Field(default="", alias="envId")
    active_dir: str = Field(default="active", alias="activeDir")
    max_concurrent_envs: str = Field(default="32", alias="maxConcurrentEnvs")

    model_config = {"populate_by_name": True}


class VmImageCacheConfig(BaseModel):
    namespace: str = "default"
    bucket: str = ""
    region: str = "us-west-2"
    image: str = "peakcom/s5cmd:v2.3.0"
    pause_image: str = Field(default="registry.k8s.io/pause:3.10", alias="pauseImage")
    host_path: str = Field(default="/var/lib/grl", alias="hostPath")

    model_config = {"populate_by_name": True}


class ModelCacheConfig(BaseModel):
    namespace: str = "default"
    tag: str = ""
    revision: str = ""
    huggingface_token: str = Field(default="", alias="huggingfaceToken")
    image: str = "python:3.12-slim"
    pause_image: str = Field(default="registry.k8s.io/pause:3.10", alias="pauseImage")
    host_path: str = Field(default="/models", alias="hostPath")

    model_config = {"populate_by_name": True}

    @property
    def enabled(self) -> bool:
        return bool(self.tag)

    def local_model_name(self) -> str:
        if not self.tag:
            raise ValueError("model_cache.tag is required")
        return self.tag.rsplit("/", 1)[-1]

    def resolved_path(self) -> str:
        return f"{self.host_path.rstrip('/')}/{self.local_model_name()}"

    def helm_fragment(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.model_dump(by_alias=True).items()
            if value != ""
        }


class InfraConfig(BaseModel):
    release_name: str = "grl-resources"
    env_release_name: str = "grl-environments"
    release_namespace: str = "default"
    cluster_name: str = "grl"
    region: str = "us-west-2"
    helm_chart_path: str = "infra/modules/resources/chart"
    ray_address: str = "ray://grl-ray-head:10001"
    ray_cluster: RayClusterConfig = Field(default_factory=RayClusterConfig)
    otel_collector: OtelCollectorConfig = Field(default_factory=OtelCollectorConfig)
    manager: ManagerConfig = Field(default_factory=ManagerConfig)
    vm_image_cache: VmImageCacheConfig = Field(default_factory=VmImageCacheConfig)
    model_cache: ModelCacheConfig = Field(default_factory=ModelCacheConfig)

    @field_validator("cluster_name")
    @classmethod
    def _validate_cluster_name(cls, value: str) -> str:
        return validate_cluster_name(value)


class GRLConfig(TrainingGRLConfig):
    """Unified run config for training and infra orchestration."""

    launch: LaunchConfig = Field(default_factory=LaunchConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    compute: ComputeConfig = Field(default_factory=ComputeConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)

    @model_validator(mode="after")
    def validate_model_cache(self) -> GRLConfig:
        cache = self.infra.model_cache
        if not cache.enabled:
            return self
        if cache.tag != self.model:
            raise ValueError(
                f"model must match infra.model_cache.tag: "
                f"expected {cache.tag!r}, got {self.model!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_deployment(self) -> GRLConfig:
        # BYOK targets a pre-existing cluster reached via kubeconfig; without one
        # there is no cluster to connect to. (The bundle_uri requirement for the
        # ENVS/TRAINING layers is enforced by preflight, so minimal configs stay
        # constructible.)
        if self.launch.is_byok() and not self.launch.infra.kubeconfig:
            raise ValueError(
                "launch.cluster_type=BYOK requires launch.infra.kubeconfig"
            )
        return self

    @model_validator(mode="after")
    def validate_compute_for_provider(self) -> GRLConfig:
        provider = provider_for_cluster_type(self.launch.cluster_type)
        if provider is not None:
            self.compute.validate_with_provider(provider)
        return self

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> GRLConfig:
        with Path(path).open() as f:
            data = yaml.safe_load(f) or {}
        data = resolve_secret_fields(data)
        return cls.model_validate(data)

    def resolve_run_id(self) -> str:
        return self.telemetry.run_id or new_run_id()

    def config_hash(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    def resolved_manager(self) -> ManagerConfig:
        manager = self.infra.manager.model_copy(deep=True)
        if not manager.bundle_uri and self.environment.bundle_uri:
            manager.bundle_uri = self.environment.bundle_uri
        if not manager.env_id and self.environment.id:
            manager.env_id = self.environment.id
        return manager

    def apply_resolved_images(self, resolved: ResolvedImages) -> None:
        self.infra.ray_cluster.images.head = resolved.head
        self.infra.ray_cluster.images.rollouts = resolved.rollouts
        self.infra.ray_cluster.images.training = resolved.training
        self.infra.manager.image = resolved.manager

    def _cluster_type(self) -> str:
        return self.launch.cluster_type

    def rollouts_gpus_per_node(self) -> int:
        return self.compute.resolved_gpus_per_node("rollouts", self._cluster_type())

    def training_gpus_per_node(self) -> int:
        return self.compute.resolved_gpus_per_node("training", self._cluster_type())

    def rollout_gpus_total(self) -> int:
        return self.compute.gpu_capacity("rollouts", self._cluster_type())

    def training_gpus_total(self) -> int:
        return self.compute.gpu_capacity("training", self._cluster_type())

    def derived_num_rollout_workers(self) -> int:
        tp = self.rollout.tensor_parallel_size
        return self.rollout_gpus_total() // tp

    def resolved_num_rollout_workers(self) -> int:
        if self.workers.num_rollout_workers is not None:
            return self.workers.num_rollout_workers
        return self.derived_num_rollout_workers()

    def helm_values_overlay(self) -> dict[str, Any]:
        """Values overlay for the Terraform-owned ``resources`` chart.

        The per-run bundle (``bundleUri``) lives in the separate, launcher-owned
        ``environments`` chart (see :meth:`env_helm_values`), so it is absent
        here — the resources chart is stable across runs.
        """
        manager = self.resolved_manager()
        overlay: dict[str, Any] = {
            "manager": {
                "envId": manager.env_id,
                "image": manager.image,
            },
            "rayCluster": {
                "images": {
                    "head": self.infra.ray_cluster.images.head,
                    "rollouts": self.infra.ray_cluster.images.rollouts,
                    "training": self.infra.ray_cluster.images.training,
                },
                "workers": {
                    "rollouts": {
                        "gpusPerNode": self.rollouts_gpus_per_node(),
                        "replicas": self.compute.rollouts.nodes,
                        "vllmMetricsPort": self.rollout.vllm_metrics_port,
                    },
                    "training": {
                        "gpusPerNode": self.training_gpus_per_node(),
                        "replicas": self.compute.training.nodes,
                    },
                },
            },
        }
        if self.infra.vm_image_cache.bucket:
            overlay["vmImageCache"] = {
                "bucket": self.infra.vm_image_cache.bucket,
            }
        if self.infra.model_cache.enabled:
            overlay["modelCache"] = self.infra.model_cache.helm_fragment()
        return overlay

    def env_helm_values(self) -> dict[str, Any]:
        """Values for the launcher-owned ``environments`` chart (bundle-sync).

        ``hostPath``/``activeDir`` must match the resources chart so the manager
        reads what the bundle-sync DaemonSet writes.
        """
        manager = self.resolved_manager()
        cache = self.infra.vm_image_cache
        return {
            "bundleUri": manager.bundle_uri,
            "envId": manager.env_id,
            "activeDir": self.infra.manager.active_dir,
            "namespace": cache.namespace,
            "hostPath": cache.host_path,
            "region": cache.region,
            "image": cache.image,
        }

    def terraform_vars(self, resolved: ResolvedImages) -> dict[str, Any]:
        vars_: dict[str, Any] = {
            "cluster_name": self.infra.cluster_name,
            "region": self.infra.region,
            "deploy_workloads": self.launch.runs_resources(),
            "node_groups": self.compute.node_groups_terraform_value(self._cluster_type()),
            "ray_cluster_name": self.infra.ray_cluster.name,
            "ray_cluster_namespace": self.infra.ray_cluster.namespace,
            "ray_head_image": resolved.head,
            "ray_rollouts_image": resolved.rollouts,
            "ray_training_image": resolved.training,
            "ray_version": self.infra.ray_cluster.version,
            "ray_rollouts_gpus_per_node": self.rollouts_gpus_per_node(),
            "ray_training_gpus_per_node": self.training_gpus_per_node(),
            "ray_rollouts_replicas": self.compute.rollouts.nodes,
            "ray_training_replicas": self.compute.training.nodes,
            "manager_image": resolved.manager,
            "release_name": self.infra.release_name,
            "release_namespace": self.infra.release_namespace,
            "vm_images_bucket": self.infra.vm_image_cache.bucket,
            "vm_images_region": self.infra.vm_image_cache.region,
            "otel_collector_name": self.infra.otel_collector.name,
            "otel_collector_namespace": self.infra.otel_collector.namespace,
            "otel_upstream_endpoint": self.infra.otel_collector.upstream.endpoint,
            "otel_upstream_username": self.infra.otel_collector.upstream.username,
            "otel_upstream_password": self.infra.otel_collector.upstream.password,
        }
        if self.infra.model_cache.enabled:
            vars_["model_tag"] = self.infra.model_cache.tag
            if self.infra.model_cache.revision:
                vars_["model_revision"] = self.infra.model_cache.revision
            if self.infra.model_cache.huggingface_token:
                vars_["huggingface_token"] = self.infra.model_cache.huggingface_token
        return {
            key: value
            for key, value in vars_.items()
            if value != "" and value is not None
        }

    def byok_terraform_vars(self, resolved: ResolvedImages) -> dict[str, Any]:
        vars_ = dict(self.terraform_vars(resolved))
        if self.launch.infra.kubeconfig:
            vars_["kubeconfig_path"] = str(self.launch.infra.resolved_kubeconfig())
        return vars_

    def terraform_vars_for_teardown(self, resolved: ResolvedImages) -> dict[str, Any]:
        vars_ = self.terraform_vars(resolved)
        vars_["deploy_workloads"] = True
        return vars_

    def byok_terraform_vars_for_teardown(self, resolved: ResolvedImages) -> dict[str, Any]:
        vars_ = self.byok_terraform_vars(resolved)
        vars_["deploy_workloads"] = True
        return vars_

    def training_payload(self, run_id: str | None = None) -> dict[str, Any]:
        payload = self.model_dump(
            mode="json",
            include={
                "model",
                "grpo",
                "workers",
                "rollout",
                "pipeline",
                "environment",
                "telemetry",
                "checkpoint",
                "ray",
            }
        )
        resolved_run_id = run_id or self.resolve_run_id()
        payload["telemetry"] = {**payload.get("telemetry", {}), "run_id": resolved_run_id}
        payload["workers"] = {
            **payload.get("workers", {}),
            "num_rollout_workers": self.resolved_num_rollout_workers(),
        }
        return payload

    def training_yaml(self, run_id: str | None = None) -> str:
        return yaml.safe_dump(self.training_payload(run_id=run_id), sort_keys=False)


ENV_REF_PATTERN = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_env_ref(value: str) -> str:
    """Expand ``${env:VAR_NAME}`` to the corresponding environment value."""
    match = ENV_REF_PATTERN.match(value.strip())
    if not match:
        return value
    var_name = match.group(1)
    env_value = os.environ.get(var_name)
    if env_value is None:
        raise ValueError(f"environment variable {var_name!r} is not set")
    return env_value


def resolve_secret_fields(data: object) -> object:
    """Recursively resolve ``${env:...}`` strings in config dicts."""
    if isinstance(data, dict):
        return {key: resolve_secret_fields(value) for key, value in data.items()}
    if isinstance(data, list):
        return [resolve_secret_fields(item) for item in data]
    if isinstance(data, str):
        return resolve_env_ref(data)
    return data


def load_config(path: str | Path) -> GRLConfig:
    return GRLConfig.from_yaml(path)
