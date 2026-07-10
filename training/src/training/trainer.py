from __future__ import annotations

from typing import TYPE_CHECKING, Any

import ray

if TYPE_CHECKING:
    import torch

from grl_config.training import GRLConfig
from training.telemetry import (
    counter,
    gauge,
    histogram,
    init_telemetry,
    record_duration,
    span,
)
from training.types import PolicyWeightsRef, RolloutResult, TrainingBatch

# Re-export for callers that historically imported TrainingBatch from here.
__all__ = ["TrainingBatch", "TrainingWorker", "grpo_valid_rollouts"]


def grpo_valid_rollouts(
    group: list[RolloutResult],
    *,
    min_rollouts_per_group: int,
) -> list[RolloutResult]:
    """Return rollouts eligible for GRPO advantage computation."""
    valid = [
        r
        for r in group
        if r.done_reason != "infra_error" and r.reward is not None
    ]
    if len(valid) < min_rollouts_per_group:
        return []
    return valid


@ray.remote
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

        from training.checkpoints import BackgroundCheckpointUploader

        model_path = cfg.resolved_model_path()

        self.beta = cfg.grpo.beta
        self.epsilon = cfg.grpo.epsilon
        self.loss_scale_factor = cfg.grpo.loss_scale_factor
        learning_rate = cfg.grpo.learning_rate
        self.min_rollouts_per_group = cfg.grpo.min_rollouts_per_group
        self.run_id = run_id
        self.checkpoint_bucket_uri = cfg.checkpoint.bucket_uri
        self.checkpoint_interval_steps = cfg.checkpoint.interval_steps
        self.checkpoint_staging_dir = cfg.checkpoint.staging_dir
        self.checkpoint_uploader = (
            BackgroundCheckpointUploader(
                bucket_uri=cfg.checkpoint.bucket_uri,
                max_background_uploads=cfg.checkpoint.max_background_uploads,
            )
            if cfg.checkpoint.bucket_uri
            else None
        )
        self.checkpoint_uris: dict[int, str] = {}

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
    ) -> int | None:
        import torch

        with span(
            "train_batch",
            batch_id=batch.batch_id,
            policy_version=batch.policy_version,
            num_groups=len(batch.groups),
        ) as current:
            rollouts, advantages, rewards = self._flatten_rollouts(batch.groups)
            if not rollouts:
                return None

            current.set_attribute("num_rollouts", len(rollouts))
            self.optimizer.zero_grad()
            tensors = self._collate_rollouts(rollouts, advantages)
            with record_duration("grl.train.step.duration"):
                trainer_logprobs, mean_entropy = self._get_logprobs(
                    tensors["input_ids"],
                    tensors["attention_mask"],
                    tensors["prompt_lens"],
                    tensors["response_lens"],
                )
                loss, stats = self._compute_loss(
                    trainer_logprobs=trainer_logprobs,
                    inference_logprobs=tensors["inference_logprobs"],
                    advantages=tensors["advantages"],
                    mask=tensors["response_mask"],
                )
                loss.backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), float("inf")
                )
                self.optimizer.step()

            self.policy_version = max(self.policy_version, batch.policy_version) + 1
            self._record_train_metrics(
                loss=float(loss.item()),
                stats=stats,
                grad_norm=float(grad_norm),
                mean_entropy=mean_entropy,
                tensors=tensors,
                advantages=advantages,
                rewards=rewards,
                num_rollouts=len(rollouts),
            )
            current.set_attribute("loss", float(loss.item()))

            with span("weight_sync"), record_duration("grl.train.weight_sync.duration"):
                weights_ref = self.send_weights()
                update_refs = []
                for worker in rollout_workers:
                    update_refs.append(
                        worker.apply_policy_update.remote(
                            self.policy_version, weights_ref
                        )
                    )
                ray.get(update_refs)
            self.checkpoint()
            return self.policy_version

    def save_checkpoint(self) -> str:
        return self.checkpoint(final=True)

    def checkpoint(self, *, final: bool = False) -> str | None:
        if self.checkpoint_uploader is None:
            if final:
                raise RuntimeError("checkpoint.bucket_uri is required to save checkpoints")
            return None
        self.checkpoint_uploader.check_completed()
        should_snapshot = final
        if self.checkpoint_interval_steps is not None:
            should_snapshot = (
                should_snapshot
                or self.policy_version % self.checkpoint_interval_steps == 0
            )
        if not should_snapshot:
            return None
        if self.policy_version not in self.checkpoint_uris:
            from training.checkpoints import snapshot_checkpoint_dir

            checkpoint_dir = snapshot_checkpoint_dir(
                model=self.model,
                tokenizer=self.tokenizer,
                staging_dir=self.checkpoint_staging_dir,
                run_id=self.run_id,
                policy_version=self.policy_version,
            )
            self.checkpoint_uris[self.policy_version] = self.checkpoint_uploader.enqueue(
                checkpoint_dir,
                run_id=self.run_id,
                policy_version=self.policy_version,
            )
        if final:
            self.checkpoint_uploader.wait_all()
        return self.checkpoint_uris[self.policy_version]

    def _record_train_metrics(
        self,
        *,
        loss: float,
        stats: dict[str, float],
        grad_norm: float,
        mean_entropy: float,
        tensors: dict[str, "torch.Tensor"],
        advantages: list["torch.Tensor"],
        rewards: list[float],
        num_rollouts: int,
    ) -> None:
        pv = {"policy_version": self.policy_version}
        counter("grl.train.batches").add(1, pv)
        counter("grl.train.tokens").add(int(tensors["response_mask"].sum().item()), pv)
        gauge("grl.train.loss").set(loss, pv)
        gauge("grl.train.pg_loss").set(stats["pg_loss"], pv)
        gauge("grl.train.kl").set(stats["kl"], pv)
        gauge("grl.train.entropy").set(mean_entropy, pv)
        gauge("grl.train.clip_fraction").set(stats["clip_fraction"], pv)
        gauge("grl.train.ratio_mean").set(stats["ratio_mean"], pv)
        gauge("grl.train.grad_norm").set(grad_norm, pv)
        gauge("grl.train.rollouts_used").set(num_rollouts, pv)
        gauge("grl.train.policy_version").set(self.policy_version)

        adv_hist = histogram("grl.train.advantage")
        for advantage in advantages:
            adv_hist.record(float(advantage))
        reward_hist = histogram("grl.train.reward")
        for reward in rewards:
            reward_hist.record(reward)

    def _flatten_rollouts(
        self,
        groups: list[list[RolloutResult]],
    ) -> tuple[list[RolloutResult], list[torch.Tensor], list[float]]:
        rollouts: list[RolloutResult] = []
        advantages: list[torch.Tensor] = []
        rewards: list[float] = []

        for group in groups:
            valid = grpo_valid_rollouts(
                group, min_rollouts_per_group=self.min_rollouts_per_group
            )
            if not valid:
                # Distinguish a group that had no gradeable rollouts at all from
                # one that simply fell below the GRPO minimum, so the dashboard
                # can tell infra loss apart from sparse-reward attrition.
                gradeable = [
                    r
                    for r in group
                    if r.done_reason != "infra_error" and r.reward is not None
                ]
                reason = "all_infra" if not gradeable else "below_min"
                counter("grl.train.groups_dropped").add(1, {"reason": reason})
                continue

            group_advantages = self._compute_group_advantages([r.reward for r in valid])
            for rollout, advantage in zip(valid, group_advantages, strict=True):
                if not rollout.response_ids:
                    continue
                if len(rollout.inference_logprobs) != len(rollout.response_ids):
                    raise ValueError(
                        f"inference_logprobs length {len(rollout.inference_logprobs)} "
                        f"!= response_ids length {len(rollout.response_ids)}"
                    )
                rollouts.append(rollout)
                advantages.append(advantage)
                if rollout.reward is not None:
                    rewards.append(float(rollout.reward))

        return rollouts, advantages, rewards

    def _compute_group_advantages(self, rewards: list[float | None]) -> torch.Tensor:
        """
        DrGRPO per-rollout scalar advantages: center rewards by the group mean
        without standard-deviation scaling.
        """
        import torch

        values = [float(r) if r is not None else 0.0 for r in rewards]
        rewards_t = torch.tensor(values, dtype=torch.float32, device=self.model.device)
        return rewards_t - rewards_t.mean()

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
    ) -> tuple["torch.Tensor", float]:
        """Per-response-token logprobs and mean token entropy over the batch."""
        import torch.nn.functional as F

        outputs = self.model(input_ids, attention_mask=attention_mask)
        logits = outputs.logits.float()
        shifted_logits = logits[:, :-1, :]
        shifted_labels = input_ids[:, 1:]
        log_probs = F.log_softmax(shifted_logits, dim=-1)
        per_token_entropy = -(log_probs.exp() * log_probs).sum(dim=-1)
        gathered = log_probs.gather(-1, shifted_labels.unsqueeze(-1)).squeeze(-1)

        batch_size, max_resp = input_ids.shape[0], response_lens.max().item()
        trainer_logprobs = torch.zeros(
            batch_size,
            max_resp,
            dtype=gathered.dtype,
            device=gathered.device,
        )
        entropy_sum = 0.0
        token_count = 0
        for i in range(batch_size):
            prompt_len = int(prompt_lens[i])
            resp_len = int(response_lens[i])
            start = prompt_len - 1
            end = prompt_len + resp_len - 1
            trainer_logprobs[i, :resp_len] = gathered[i, start:end]
            if resp_len > 0:
                entropy_sum += float(per_token_entropy[i, start:end].sum().item())
                token_count += resp_len

        mean_entropy = entropy_sum / token_count if token_count else 0.0
        return trainer_logprobs, mean_entropy

    def _compute_loss(
        self,
        trainer_logprobs: torch.Tensor,
        inference_logprobs: torch.Tensor,
        advantages: torch.Tensor,
        mask: torch.Tensor,
    ) -> tuple[torch.Tensor, dict[str, float]]:
        """
        DrGRPO clipped policy loss with KL against rollout-time logprobs
        (prime-rl inference_logprobs path; beta is always applied).

        Token losses are summed and normalized by batch size times a response
        length constant, avoiding per-rollout token-mean length bias. When
        ``loss_scale_factor`` is unset, use the current padded response width.

        Returns the differentiable loss plus a dict of scalar telemetry stats
        (policy-gradient term, KL, mean ratio, clip fraction) computed from the
        same tensors so observability adds no extra forward pass.
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
        loss_scale = self.loss_scale_factor or mask.shape[1]
        loss = masked.sum() / (mask.shape[0] * loss_scale)
        stats = self._loss_stats(per_token_pg, per_token_kl, ratio, mask)
        return loss, stats

    def _loss_stats(
        self,
        per_token_pg: torch.Tensor,
        per_token_kl: torch.Tensor,
        ratio: torch.Tensor,
        mask: torch.Tensor,
    ) -> dict[str, float]:
        """Mask-weighted scalar summaries of the loss components for telemetry."""
        import torch

        with torch.no_grad():
            mask_f = mask.to(ratio.dtype)
            denom = mask_f.sum().clamp(min=1.0)
            pg = (per_token_pg * mask_f).sum() / denom
            kl = (per_token_kl * mask_f).sum() / denom
            ratio_mean = (ratio * mask_f).sum() / denom
            clipped = (
                (ratio < 1 - self.epsilon) | (ratio > 1 + self.epsilon)
            ) & mask.bool()
            clip_fraction = clipped.to(ratio.dtype).sum() / denom
        return {
            "pg_loss": float(pg.item()),
            "kl": float(kl.item()),
            "ratio_mean": float(ratio_mean.item()),
            "clip_fraction": float(clip_fraction.item()),
        }

    def _compute_kl(
        self,
        trainer_logprobs: torch.Tensor,
        inference_logprobs: torch.Tensor,
    ) -> torch.Tensor:
        """K3 KL estimator against rollout-time (inference) logprobs."""
        import torch

        log_ratio = inference_logprobs - trainer_logprobs
        return torch.exp(log_ratio) - log_ratio - 1

    def send_weights(self) -> PolicyWeightsRef:
        # TODO: Determine if we can use NCCL on A10 in AWS
        # Determine if we can send just the weight diff like Cursor does
        import torch

        with torch.no_grad():
            state_dict = {
                name: tensor.detach().to("cpu", copy=True)
                for name, tensor in self.model.state_dict().items()
            }
        return PolicyWeightsRef(ray.put(state_dict))
