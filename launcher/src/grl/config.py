"""Unified launch configuration for GRL training and cluster infra."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from grl.model import local_model_path
from pydantic import BaseModel, Field, model_validator


DEFAULT_CONFIG_PATH = Path("config.yaml")


# --- Training (matches training/src/training/config.py) ---


class GRPOConfig(BaseModel):
    beta: float = 0.001
    epsilon: float = 0.2
    learning_rate: float = 1e-6
    num_rollouts: int = 8
    groups_per_batch: int = 4
    min_rollouts_per_group: int = 2
    temperature: float = 1.0
    top_p: float = 1.0


class WorkersConfig(BaseModel):
    num_rollout_workers: int = 1
    max_in_flight_rollouts: int = 32


class RolloutConfig(BaseModel):
    max_model_len: int = 8192
    max_num_seqs: int = 64
    max_concurrent_trajectories: int = 32
    max_tokens_per_turn: int = 512
    max_assistant_turns: int = 8
    enable_prefix_caching: bool = True
    vllm_metrics_port: int = 9090
    generation_timeout_secs: float = 600.0
    trajectory_timeout_secs: float = 3600.0


class PipelineConfig(BaseModel):
    pending_tasks_queue_size: int = 64
    completed_rollouts_queue_size: int = 256
    train_batches_queue_size: int = 16
    max_policy_staleness: int = 0
    seed: int = 0
    group_assembly_timeout_secs: float = 3900.0
    group_poll_interval_secs: float = 5.0


class EnvironmentRetryConfig(BaseModel):
    max_attempts: int = 10
    initial_backoff_secs: float = 0.5
    max_backoff_secs: float = 30.0


class EnvironmentRpcTimeoutsConfig(BaseModel):
    create_secs: float = 30.0
    list_tasks_secs: float = 30.0
    execute_default_secs: float = 140.0
    execute_submit_secs: float = 10.0
    execute_timeout_buffer_secs: float = 10.0
    evaluate_secs: float = 930.0
    teardown_secs: float = 30.0


class EnvironmentConfig(BaseModel):
    id: str | None = None
    bundle_uri: str | None = None
    split: str | None = None
    server_addr: str = "localhost:50051"
    tasks_uri: str | None = None
    retry: EnvironmentRetryConfig = Field(default_factory=EnvironmentRetryConfig)
    rpc_timeouts: EnvironmentRpcTimeoutsConfig = Field(
        default_factory=EnvironmentRpcTimeoutsConfig
    )

    def resolve_tasks_uri(self) -> str:
        if self.tasks_uri:
            return self.tasks_uri
        if self.bundle_uri:
            return f"{self.bundle_uri.rstrip('/')}/tasks.jsonl"
        raise ValueError("environment.bundle_uri or environment.tasks_uri is required")


class TelemetryConfig(BaseModel):
    run_id: str | None = None
    otel_endpoint: str | None = None


class RayConfig(BaseModel):
    ignore_reinit_error: bool = True


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
        """Non-empty Helm modelCache values (camelCase keys)."""
        return {
            key: value
            for key, value in self.model_dump(by_alias=True).items()
            if value != ""
        }


class InfraConfig(BaseModel):
    """Cluster and Helm settings consumed by the launcher."""

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


class GRLConfig(BaseModel):
    """Unified run config for training and infra orchestration."""

    model: str
    grpo: GRPOConfig = Field(default_factory=GRPOConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    ray: RayConfig = Field(default_factory=RayConfig)
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
        return cls.model_validate(data)

    def resolved_model_path(self, *, cache_root: str | None = None) -> Path:
        return local_model_path(self.model, cache_root=cache_root)

    def resolve_run_id(self) -> str:
        return self.telemetry.run_id or f"grl-{uuid4().hex[:12]}"

    def resolved_manager(self) -> ManagerConfig:
        manager = self.infra.manager.model_copy(deep=True)
        if not manager.bundle_uri and self.environment.bundle_uri:
            manager.bundle_uri = self.environment.bundle_uri
        if not manager.env_id and self.environment.id:
            manager.env_id = self.environment.id
        return manager

    def helm_values_overlay(self) -> dict[str, Any]:
        """Helm values fragment for per-run manager, VM cache, and model cache."""
        manager = self.resolved_manager()
        overlay: dict[str, Any] = {
            "manager": {
                "bundleUri": manager.bundle_uri,
                "envId": manager.env_id,
            }
        }
        if self.infra.vm_image_cache.bucket:
            overlay["vmImageCache"] = {
                "bucket": self.infra.vm_image_cache.bucket,
            }
        if self.infra.model_cache.enabled:
            overlay["modelCache"] = self.infra.model_cache.helm_fragment()
        return overlay

    def terraform_model_vars(self) -> dict[str, str]:
        """Terraform variables for the model-cache DaemonSet."""
        cache = self.infra.model_cache
        if not cache.enabled:
            return {}
        vars_: dict[str, str] = {"model_tag": cache.tag}
        if cache.revision:
            vars_["model_revision"] = cache.revision
        if cache.huggingface_token:
            vars_["huggingface_token"] = cache.huggingface_token
        return vars_

    def training_payload(self) -> dict[str, Any]:
        """Dict suitable for Ray training workers (excludes infra-only fields)."""
        return self.model_dump(
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


def load_config(path: str | Path) -> GRLConfig:
    return GRLConfig.from_yaml(path)
