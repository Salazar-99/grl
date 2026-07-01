"""Unified launch configuration for GRL training and cluster infra."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from grl.secrets import resolve_secret_fields
from grl_config.run_id import new_run_id
from grl_config.training import DEFAULT_CONFIG_PATH, GRLConfig as TrainingGRLConfig
from pydantic import BaseModel, Field, model_validator


class LaunchToolsConfig(BaseModel):
    auto_install: bool = True
    terraform: Literal["terraform", "tofu"] = "terraform"
    terraform_version: str = "1.13.0"
    helm_version: str = "3.16.4"
    kubectl_version: str = "1.32.0"


class LaunchInfraConfig(BaseModel):
    apply: bool = False
    terraform_dir: str = "infra"
    auto_kubeconfig: bool = True


class LaunchEnvironmentConfig(BaseModel):
    activate: bool = True
    verify: bool = True
    refresh_vm_cache: Literal["auto", "always", "never"] = "auto"
    purge_cache: bool = False


class LaunchJobConfig(BaseModel):
    submit: bool = True
    force: bool = False
    wait: bool = False
    backend: Literal["rayjob"] = "rayjob"


class LaunchConfig(BaseModel):
    dry_run: bool = False
    preflight_only: bool = False
    tools: LaunchToolsConfig = Field(default_factory=LaunchToolsConfig)
    infra: LaunchInfraConfig = Field(default_factory=LaunchInfraConfig)
    environment: LaunchEnvironmentConfig = Field(default_factory=LaunchEnvironmentConfig)
    job: LaunchJobConfig = Field(default_factory=LaunchJobConfig)


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
    registry: str = "ghcr.io/gerardosalazar/grl"
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


class RayClusterRolloutsWorkerConfig(BaseModel):
    gpus_per_node: int = Field(default=1, alias="gpusPerNode")
    vllm_metrics_port: int = Field(default=9090, alias="vllmMetricsPort")

    model_config = {"populate_by_name": True}


class RayClusterTrainingWorkerConfig(BaseModel):
    gpus_per_node: int = Field(default=1, alias="gpusPerNode")

    model_config = {"populate_by_name": True}


class RayClusterWorkersConfig(BaseModel):
    rollouts: RayClusterRolloutsWorkerConfig = Field(
        default_factory=RayClusterRolloutsWorkerConfig
    )
    training: RayClusterTrainingWorkerConfig = Field(
        default_factory=RayClusterTrainingWorkerConfig
    )


class RayClusterConfig(BaseModel):
    name: str = "grl-ray"
    namespace: str = "default"
    version: str = "2.55.1"
    images: RayClusterImagesConfig = Field(default_factory=RayClusterImagesConfig)
    workers: RayClusterWorkersConfig = Field(default_factory=RayClusterWorkersConfig)


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


class DcgmExporterConfig(BaseModel):
    namespace: str = "default"
    image: str = "nvcr.io/nvidia/k8s/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04"


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
    release_namespace: str = "default"
    cluster_name: str = "grl"
    region: str = "us-west-2"
    helm_chart_path: str = "infra/modules/resources/chart"
    ray_address: str = "ray://grl-ray-head:10001"
    ray_cluster: RayClusterConfig = Field(default_factory=RayClusterConfig)
    otel_collector: OtelCollectorConfig = Field(default_factory=OtelCollectorConfig)
    dcgm_exporter: DcgmExporterConfig = Field(default_factory=DcgmExporterConfig)
    manager: ManagerConfig = Field(default_factory=ManagerConfig)
    vm_image_cache: VmImageCacheConfig = Field(default_factory=VmImageCacheConfig)
    model_cache: ModelCacheConfig = Field(default_factory=ModelCacheConfig)


class GRLConfig(TrainingGRLConfig):
    """Unified run config for training and infra orchestration."""

    launch: LaunchConfig = Field(default_factory=LaunchConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
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

    def helm_values_overlay(self) -> dict[str, Any]:
        manager = self.resolved_manager()
        overlay: dict[str, Any] = {
            "manager": {
                "bundleUri": manager.bundle_uri,
                "envId": manager.env_id,
                "image": manager.image,
            },
            "rayCluster": {
                "images": {
                    "head": self.infra.ray_cluster.images.head,
                    "rollouts": self.infra.ray_cluster.images.rollouts,
                    "training": self.infra.ray_cluster.images.training,
                }
            },
        }
        if self.infra.vm_image_cache.bucket:
            overlay["vmImageCache"] = {
                "bucket": self.infra.vm_image_cache.bucket,
            }
        if self.infra.model_cache.enabled:
            overlay["modelCache"] = self.infra.model_cache.helm_fragment()
        return overlay

    def terraform_vars(self, resolved: ResolvedImages) -> dict[str, str]:
        vars_: dict[str, str] = {
            "cluster_name": self.infra.cluster_name,
            "region": self.infra.region,
            "ray_cluster_name": self.infra.ray_cluster.name,
            "ray_cluster_namespace": self.infra.ray_cluster.namespace,
            "ray_head_image": resolved.head,
            "ray_rollouts_image": resolved.rollouts,
            "ray_training_image": resolved.training,
            "ray_version": self.infra.ray_cluster.version,
            "manager_image": resolved.manager,
            "vm_images_bucket": self.infra.vm_image_cache.bucket,
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
        return {key: value for key, value in vars_.items() if value != ""}

    def training_payload(self, run_id: str | None = None) -> dict[str, Any]:
        payload = self.model_dump(
            include={
                "model",
                "grpo",
                "workers",
                "rollout",
                "pipeline",
                "environment",
                "telemetry",
                "ray",
            }
        )
        resolved_run_id = run_id or self.resolve_run_id()
        payload["telemetry"] = {**payload.get("telemetry", {}), "run_id": resolved_run_id}
        return payload

    def training_yaml(self, run_id: str | None = None) -> str:
        return yaml.safe_dump(self.training_payload(run_id=run_id), sort_keys=False)


def load_config(path: str | Path) -> GRLConfig:
    return GRLConfig.from_yaml(path)
