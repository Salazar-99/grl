import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ray
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


@dataclass
class TrainingBatch:
    batch_id: str
    groups: list[list[Any]]
    policy_version: int


@dataclass
class PolicyUpdate:
    policy_version: int
    weights_ref: ray.ObjectRef | None = None


@ray.remote(num_gpus=1, resources={"training": 1})
class TrainingWorker:
    """
    Take a group of rollouts and their rewards.
    Run GRPO to update weights.
    Send weights to the rollout worker using object store.
    """

    def __init__(self) -> None:
        model_path = Path(os.environ.get("MODEL_PATH", "/models/Qwen2.5-7B"))

        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.tokenizer.pad_token = self.tokenizer.eos_token

        # Load the base policy model
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map="cuda:0",  # Put the whole model on GPU 0
            local_files_only=True,
        )

        # Load the reference model (Must be frozen/eval mode)
        self.ref_model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map="cuda:1",  # Put the reference model on a separate GPU to avoid OOM
            local_files_only=True,
        )
        self.ref_model.eval()
        self.policy_version = 0

    def train_batch(
        self,
        batch: TrainingBatch,
        rollout_workers: list[ray.actor.ActorHandle],
    ) -> PolicyUpdate:
        # TODO: Add GRPO code
        self.policy_version = max(self.policy_version, batch.policy_version) + 1
        update = PolicyUpdate(
            policy_version=self.policy_version,
            weights_ref=self.send_weights(),
        )

        for worker in rollout_workers:
            worker.apply_policy_update.remote(update.policy_version, update.weights_ref)

        return update

    def send_weights(self) -> ray.ObjectRef | None:
        # TODO: Determine if we can use NCCL on A10 in AWS
        # Determine if we can send just the weight diff like Cursor does
        return None
