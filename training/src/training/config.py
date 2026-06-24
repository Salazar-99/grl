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
    seed: int = 0


class DatasetConfig(BaseModel):
    tasks_s3_uri: str | None = None
    split: str | None = None


class EnvironmentConfig(BaseModel):
    server_addr: str = "localhost:50051"


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
    dataset: DatasetConfig = Field(default_factory=DatasetConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    ray: RayConfig = Field(default_factory=RayConfig)

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> GRLConfig:
        with Path(path).open() as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def resolve_run_id(self) -> str:
        return self.telemetry.run_id or new_run_id()
