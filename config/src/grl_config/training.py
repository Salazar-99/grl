"""Central configuration for the GRL training loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from grl_config.model import local_model_path
from grl_config.run_id import new_run_id

DEFAULT_CONFIG_PATH = Path("config.yaml")


class GRPOConfig(BaseModel):
    beta: float = 0.001
    epsilon: float = 0.2
    learning_rate: float = 1e-6
    loss_scale_factor: int | None = None
    num_rollouts: int = 8
    groups_per_batch: int = 4
    min_rollouts_per_group: int = 2
    temperature: float = 1.0
    top_p: float = 1.0

    def sampling_params(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "top_p": self.top_p}


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
    # Wall-clock cap on one vLLM completion (prime-rl SWE runs rely on per-turn
    # token limits; this bounds hung generation).
    generation_timeout_secs: float = 600.0
    # Per-trajectory wall clock (prime-rl eval SWE configs use timeout.rollout=3600).
    trajectory_timeout_secs: float = 3600.0


class PipelineConfig(BaseModel):
    pending_tasks_queue_size: int = 64
    completed_rollouts_queue_size: int = 256
    train_batches_queue_size: int = 16
    max_train_steps: int | None = Field(None, ge=1)
    """Maximum successful policy updates before training exits. None runs indefinitely."""
    # Flush a partial training batch when the oldest queued group is this many
    # policy versions behind the newest completed rollout.
    max_policy_staleness: int = 0
    seed: int = 0
    # Emit partial GRPO groups when not all rollouts finish in time.
    group_assembly_timeout_secs: float = 3900.0
    group_poll_interval_secs: float = 5.0


class EnvironmentRetryConfig(BaseModel):
    max_attempts: int = 10
    initial_backoff_secs: float = 0.5
    max_backoff_secs: float = 30.0


class EnvironmentRpcTimeoutsConfig(BaseModel):
    """Client-side gRPC deadlines for manager calls (seconds).

    Defaults align with in-VM bash/score timeouts and prime-rl SWE reference
    configs (sandbox_command_timeout=30, eval rollout timeout=3600, score ~900s).
    """

    create_secs: float = 30.0
    list_tasks_secs: float = 30.0
    execute_default_secs: float = 140.0
    execute_submit_secs: float = 10.0
    execute_timeout_buffer_secs: float = 10.0
    evaluate_secs: float = 930.0
    teardown_secs: float = 30.0


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

    retry: EnvironmentRetryConfig = Field(default_factory=EnvironmentRetryConfig)
    """gRPC retry policy for manager calls (admission, env boot)."""

    rpc_timeouts: EnvironmentRpcTimeoutsConfig = Field(
        default_factory=EnvironmentRpcTimeoutsConfig
    )
    """Per-RPC client deadlines passed to gRPC stubs."""

    def resolve_tasks_uri(self) -> str:
        if self.tasks_uri:
            return self.tasks_uri
        if self.bundle_uri:
            return f"{self.bundle_uri.rstrip('/')}/tasks.jsonl"
        raise ValueError("environment.bundle_uri or environment.tasks_uri is required")


class TelemetryConfig(BaseModel):
    run_id: str | None = None
    otel_endpoint: str | None = None


class CheckpointConfig(BaseModel):
    bucket_uri: str | None = None
    """Object storage URI where final training checkpoints are written."""
    interval_steps: int | None = Field(None, ge=1)
    """Save and upload a background checkpoint every N successful policy updates."""
    staging_dir: Path = Path("/tmp/grl-checkpoints")
    """Local directory used to stage immutable checkpoints before upload."""
    max_background_uploads: int = Field(1, ge=1)
    """Maximum checkpoint uploads allowed to run in the background."""


class RayConfig(BaseModel):
    ignore_reinit_error: bool = True


class GRLConfig(BaseModel):
    model: str
    grpo: GRPOConfig = Field(default_factory=GRPOConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    ray: RayConfig = Field(default_factory=RayConfig)

    @model_validator(mode="after")
    def validate_checkpoint_destination(self) -> GRLConfig:
        checkpointing_enabled = (
            self.pipeline.max_train_steps is not None
            or self.checkpoint.interval_steps is not None
        )
        if checkpointing_enabled and not self.checkpoint.bucket_uri:
            raise ValueError(
                "checkpoint.bucket_uri is required when checkpointing is enabled"
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
        return self.telemetry.run_id or new_run_id()
