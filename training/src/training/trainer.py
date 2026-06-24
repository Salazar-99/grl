from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ray

if TYPE_CHECKING:
    import torch

from training.config import GRLConfig
from training.rollouts import RolloutResult
from training.telemetry import init_telemetry


@dataclass
class TrainingBatch:
    batch_id: str
    groups: list[list[RolloutResult]]
    policy_version: int


@ray.remote(num_gpus=1, resources={"training": 1})
class TrainingWorker:
    """
    Take a group of rollouts and their rewards.
    Run GRPO to update weights.
    Send weights to the rollout worker using object store.
    """

    def __init__(self, config: dict[str, Any], *, run_id: str = "") -> None:
        cfg = GRLConfig.model_validate(config)
        init_telemetry(
            "training",
            run_id,
            otel_endpoint=cfg.telemetry.otel_endpoint,
        )

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_path = Path(cfg.model.path)

        self.beta = cfg.grpo.beta
        self.epsilon = cfg.grpo.epsilon
        learning_rate = cfg.grpo.learning_rate

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, local_files_only=True
        )
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.pad_token_id = int(self.tokenizer.pad_token_id)

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map="cuda:0",
            local_files_only=True,
        )
        self.model.train()

        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=learning_rate)
        self.policy_version = 0

    def train_batch(
        self,
        batch: TrainingBatch,
        rollout_workers: list[ray.actor.ActorHandle],
    ) -> None:
        rollouts, advantages = self._flatten_rollouts(batch.groups)
        if not rollouts:
            return

        self.optimizer.zero_grad()
        tensors = self._collate_rollouts(rollouts, advantages)
        trainer_logprobs = self._get_logprobs(
            tensors["input_ids"],
            tensors["attention_mask"],
            tensors["prompt_lens"],
            tensors["response_lens"],
        )
        loss = self._compute_loss(
            trainer_logprobs=trainer_logprobs,
            inference_logprobs=tensors["inference_logprobs"],
            advantages=tensors["advantages"],
            mask=tensors["response_mask"],
        )
        loss.backward()
        self.optimizer.step()

        self.policy_version = max(self.policy_version, batch.policy_version) + 1
        weights_ref = self.send_weights()

        for worker in rollout_workers:
            worker.apply_policy_update.remote(self.policy_version, weights_ref)

    def _flatten_rollouts(
        self,
        groups: list[list[RolloutResult]],
    ) -> tuple[list[RolloutResult], list[torch.Tensor]]:
        rollouts: list[RolloutResult] = []
        advantages: list[torch.Tensor] = []

        for group in groups:
            group_advantages = self._compute_group_advantages([r.reward for r in group])
            for rollout, advantage in zip(group, group_advantages, strict=True):
                if not rollout.response_ids:
                    continue
                if len(rollout.inference_logprobs) != len(rollout.response_ids):
                    raise ValueError(
                        f"inference_logprobs length {len(rollout.inference_logprobs)} "
                        f"!= response_ids length {len(rollout.response_ids)}"
                    )
                rollouts.append(rollout)
                advantages.append(advantage)

        return rollouts, advantages

    def _compute_group_advantages(self, rewards: list[float | None]) -> torch.Tensor:
        """
        Per-rollout scalar advantages for a GRPO group: center rewards by the
        group mean and scale by the group standard deviation.
        """
        import torch

        values = [float(r) if r is not None else 0.0 for r in rewards]
        rewards_t = torch.tensor(values, dtype=torch.float32, device=self.model.device)
        mean = rewards_t.mean()
        std = rewards_t.std(unbiased=False)
        return (rewards_t - mean) / (std + 1e-4)

    def _collate_rollouts(
        self,
        rollouts: list[RolloutResult],
        advantages: list[torch.Tensor],
    ) -> dict[str, torch.Tensor]:
        import torch

        device = self.model.device
        batch_size = len(rollouts)
        max_len = max(
            len(rollout.prompt_ids) + len(rollout.response_ids) for rollout in rollouts
        )
        max_resp = max(len(rollout.response_ids) for rollout in rollouts)

        input_ids = torch.full(
            (batch_size, max_len),
            self.pad_token_id,
            dtype=torch.long,
            device=device,
        )
        attention_mask = torch.zeros(batch_size, max_len, dtype=torch.bool, device=device)
        prompt_lens = torch.zeros(batch_size, dtype=torch.long, device=device)
        response_lens = torch.zeros(batch_size, dtype=torch.long, device=device)
        inference_logprobs = torch.zeros(batch_size, max_resp, dtype=torch.float32, device=device)
        response_mask = torch.zeros(batch_size, max_resp, dtype=torch.bool, device=device)
        advantages_t = torch.stack(advantages).to(device=device, dtype=torch.float32)

        for i, rollout in enumerate(rollouts):
            seq = rollout.prompt_ids + rollout.response_ids
            seq_len = len(seq)
            input_ids[i, :seq_len] = torch.tensor(seq, dtype=torch.long, device=device)
            attention_mask[i, :seq_len] = True
            prompt_lens[i] = len(rollout.prompt_ids)
            response_lens[i] = len(rollout.response_ids)
            resp_len = len(rollout.response_ids)
            inference_logprobs[i, :resp_len] = torch.tensor(
                rollout.inference_logprobs,
                dtype=torch.float32,
                device=device,
            )
            response_mask[i, :resp_len] = torch.tensor(
                rollout.response_mask,
                dtype=torch.bool,
                device=device,
            )

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "prompt_lens": prompt_lens,
            "response_lens": response_lens,
            "inference_logprobs": inference_logprobs,
            "response_mask": response_mask,
            "advantages": advantages_t,
        }

    def _get_logprobs(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        prompt_lens: torch.Tensor,
        response_lens: torch.Tensor,
    ) -> torch.Tensor:
        """Per-response-token logprobs, padded to (batch, max_response_len)."""
        import torch.nn.functional as F

        outputs = self.model(input_ids, attention_mask=attention_mask)
        logits = outputs.logits.float()
        shifted_logits = logits[:, :-1, :]
        shifted_labels = input_ids[:, 1:]
        log_probs = F.log_softmax(shifted_logits, dim=-1)
        gathered = log_probs.gather(-1, shifted_labels.unsqueeze(-1)).squeeze(-1)

        batch_size, max_resp = input_ids.shape[0], response_lens.max().item()
        trainer_logprobs = torch.zeros(
            batch_size,
            max_resp,
            dtype=gathered.dtype,
            device=gathered.device,
        )
        for i in range(batch_size):
            prompt_len = int(prompt_lens[i])
            resp_len = int(response_lens[i])
            trainer_logprobs[i, :resp_len] = gathered[i, prompt_len - 1 : prompt_len + resp_len - 1]

        return trainer_logprobs

    def _compute_loss(
        self,
        trainer_logprobs: torch.Tensor,
        inference_logprobs: torch.Tensor,
        advantages: torch.Tensor,
        mask: torch.Tensor,
    ) -> torch.Tensor:
        """
        GRPO clipped policy loss with KL against rollout-time logprobs
        (prime-rl inference_logprobs path; beta is always applied).

        Each rollout is normalized over its own masked response tokens, then
        averaged across the batch (one backward pass over the full batch).
        """
        import torch

        log_ratio = trainer_logprobs - inference_logprobs
        ratio = torch.exp(log_ratio)
        clipped_ratio = torch.clamp(ratio, 1 - self.epsilon, 1 + self.epsilon)

        adv = advantages.unsqueeze(1).to(trainer_logprobs.dtype)
        pg_loss1 = ratio * adv
        pg_loss2 = clipped_ratio * adv
        per_token_pg = -torch.min(pg_loss1, pg_loss2)

        per_token_kl = self._compute_kl(trainer_logprobs, inference_logprobs)
        per_token_loss = per_token_pg + self.beta * per_token_kl

        masked = per_token_loss * mask
        per_rollout_loss = masked.sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        return per_rollout_loss.mean()

    def _compute_kl(
        self,
        trainer_logprobs: torch.Tensor,
        inference_logprobs: torch.Tensor,
    ) -> torch.Tensor:
        """K3 KL estimator against rollout-time (inference) logprobs."""
        import torch

        log_ratio = inference_logprobs - trainer_logprobs
        return torch.exp(log_ratio) - log_ratio - 1

    def send_weights(self) -> ray.ObjectRef | None:
        # TODO: Determine if we can use NCCL on A10 in AWS
        # Determine if we can send just the weight diff like Cursor does
        return None
