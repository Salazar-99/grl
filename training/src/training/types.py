"""Shared dataclasses used across head, rollouts, and training roles.

Kept free of optional extras (torch / transformers / vllm / renderers) so the
head driver and each worker image can import these types without pulling
role-specific implementation modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import ray


@dataclass
class ToolCall:
    name: str
    arguments: str


@dataclass
class RolloutRequest:
    group_id: str
    task_id: str
    rollout_index: int
    expected_group_size: int
    policy_version: int
    sampling_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RolloutResult:
    group_id: str
    task_id: str
    env_id: str
    rollout_index: int
    expected_group_size: int
    # Latest policy version observed while generating this rollout. For async
    # weight updates this can differ from policy_version_start.
    policy_version_current: int
    request_id: str
    prompt_ids: list[int]
    response_ids: list[int]
    response_mask: list[int]
    # Per-token logprobs from vLLM at rollout time; aligned 1:1 with response_ids.
    inference_logprobs: list[float]
    num_turns: int
    reward: float | None = None
    done_reason: str = "completed"
    policy_version_start: int | None = None


@dataclass(frozen=True)
class PolicyWeightsRef:
    """Nested wrapper so Ray sends the ObjectRef itself, not its value."""

    ref: ray.ObjectRef


@dataclass
class GenerationResult:
    token_ids: list[int]
    logprobs: list[float]


@dataclass
class TrainingBatch:
    batch_id: str
    groups: list[list[RolloutResult]]
    policy_version: int
