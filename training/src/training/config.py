"""Central configuration for the GRL training loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from training.telemetry import new_run_id


DEFAULT_CONFIG_PATH = Path("config.yaml")


class ModelConfig(BaseModel):
    path: str = "/models/Qwen2.5-7B"


class GRPOConfig(BaseModel):
    beta: float = 0.001
    epsilon: float = 0.2
    learning_rate: float = 1e-6
    num_rollouts: int = 8
    groups_per_batch: int = 4
    min_rollouts_per_group: int = 2
    temperature: float = 1.0
    top_p: float = 1.0

    def sampling_params(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "top_p": self.top_p}


class WorkersConfig(BaseModel):
    num_rollout_workers: int = 1
    num_training_workers: int = 1
    max_in_flight_rollouts: int = 32


class RolloutConfig(BaseModel):
    max_model_len: int = 8192
    max_num_seqs: int = 64
    max_concurrent_trajectories: int = 32
    max_tokens_per_turn: int = 512
    max_assistant_turns: int = 8
    enable_prefix_caching: bool = True
    vllm_metrics_port: int = 9090


class PipelineConfig(BaseModel):
    pending_tasks_queue_size: int = 64
    completed_rollouts_queue_size: int = 256
    train_batches_queue_size: int = 16
    # Flush a partial training batch when the oldest queued group is this many
    # policy versions behind the newest completed rollout.
    max_policy_staleness: int = 0
    seed: int = 0


class EnvironmentRetryConfig(BaseModel):
    max_attempts: int = 10
    initial_backoff_secs: float = 0.5
    max_backoff_secs: float = 30.0


class EnvironmentConfig(BaseModel):
    """Artifact bundle + manager connection for one RL environment.

    ``bundle_uri`` is the env's published prefix (e.g.
    ``s3://bucket/datasets/swebench-lite/dev``). Standard artifacts are derived
    from it unless overridden explicitly.
    """

    id: str | None = None
    """Environment name for logging (e.g. ``swebench-lite``). Optional."""

    bundle_uri: str | None = None
    """Root URI of the environment artifact bundle. Used by the launcher/infra
    to sync artifacts onto environment nodes; the trainer does not read it."""

    split: str | None = None
    """When set, only ``task_id`` rows with this split are used for training."""

    server_addr: str = "localhost:50051"
    """gRPC address of the environment manager."""

    tasks_uri: str | None = None
    """Override for ``{bundle_uri}/tasks.jsonl``. Launcher/infra only."""

    manifest_uri: str | None = None
    """Override for ``{bundle_uri}/manifest.json``. Launcher/infra only."""

    retry: EnvironmentRetryConfig = Field(default_factory=EnvironmentRetryConfig)
    """gRPC retry policy for manager calls (admission, env boot)."""

    def resolve_tasks_uri(self) -> str:
        if self.tasks_uri:
            return self.tasks_uri
        if self.bundle_uri:
            return f"{self.bundle_uri.rstrip('/')}/tasks.jsonl"
        raise ValueError("environment.bundle_uri or environment.tasks_uri is required")

    def resolve_manifest_uri(self) -> str | None:
        if self.manifest_uri:
            return self.manifest_uri
        if self.bundle_uri:
            return f"{self.bundle_uri.rstrip('/')}/manifest.json"
        return None


class TelemetryConfig(BaseModel):
    run_id: str | None = None
    otel_endpoint: str | None = None


class RayConfig(BaseModel):
    ignore_reinit_error: bool = True


class GRLConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    grpo: GRPOConfig = Field(default_factory=GRPOConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    ray: RayConfig = Field(default_factory=RayConfig)

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> GRLConfig:
        with Path(path).open() as f:
            data = yaml.safe_load(f) or {}
        cls._migrate_legacy_dataset_config(data)
        return cls.model_validate(data)

    @staticmethod
    def _migrate_legacy_dataset_config(data: dict[str, Any]) -> None:
        """Fold deprecated top-level ``dataset`` keys into ``environment``."""
        dataset = data.pop("dataset", None)
        if not dataset:
            return
        env = data.setdefault("environment", {})
        if not isinstance(env, dict):
            return
        if not isinstance(dataset, dict):
            return
        if tasks_uri := dataset.get("tasks_s3_uri"):
            env.setdefault("tasks_uri", tasks_uri)
        if split := dataset.get("split"):
            env.setdefault("split", split)

    def resolve_run_id(self) -> str:
        return self.telemetry.run_id or new_run_id()
