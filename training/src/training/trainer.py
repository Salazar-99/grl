from __future__ import annotations

from typing import TYPE_CHECKING, Any

import ray

if TYPE_CHECKING:
    from pathlib import Path

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


# The language stack of a checkpoint-shaped state dict. Everything else (e.g. a
# vision tower) is carried for the rollout engine's benefit but never trained.
LANGUAGE_PARAM_PREFIXES = ("model.language_model.", "model.layers.", "model.embed", "lm_head.")

# Auxiliary heads that ship in the checkpoint but that neither transformers nor the
# rollout engine instantiates for ordinary decoding. `mtp.*` is Qwen's multi-token
# prediction head, used only for speculative decoding: feeding vLLM a state dict
# without it still leaves zero parameters on meta, so its absence is not a mismatch.
IGNORED_CHECKPOINT_PREFIXES = ("mtp.",)


def load_policy_model(model_path: "str | Path") -> "torch.nn.Module":
    """Load the policy under the checkpoint's own architecture.

    This must match the architecture the rollout engine serves, because the state
    dict produced here is fed straight back into it. Reaching for
    AutoModelForCausalLM on a `*ForConditionalGeneration` checkpoint silently
    builds the text-only tower: the parameters come out named `model.*` instead of
    `model.language_model.*` and the vision tower is missing entirely. vLLM's
    reload moves every parameter to `meta` before repopulating it from the names it
    is given, so those mismatched names leave the real embedding on `meta` and the
    next forward pass dies with "Cannot copy out of meta tensor".
    """
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText

    config = AutoConfig.from_pretrained(model_path, local_files_only=True)
    architectures = getattr(config, "architectures", None) or [""]
    auto_class = (
        AutoModelForImageTextToText
        if architectures[0].endswith("ForConditionalGeneration")
        else AutoModelForCausalLM
    )
    model = auto_class.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    assert_covers_checkpoint(model, model_path)
    return model.to("cuda:0")


def assert_covers_checkpoint(model: "torch.nn.Module", model_path: "str | Path") -> None:
    """Fail fast unless the policy's state dict names every weight in the checkpoint.

    The rollout engine repopulates its parameters from these names and leaves any it
    was not given on `meta`, where they detonate on the next forward pass rather than
    at load time. Checking the invariant here turns a silent, hour-deep engine crash
    into an error before the first rollout.
    """
    import json
    from pathlib import Path

    index = Path(model_path) / "model.safetensors.index.json"
    if not index.is_file():
        return
    checkpoint_names = {
        name
        for name in json.loads(index.read_text())["weight_map"]
        if not name.startswith(IGNORED_CHECKPOINT_PREFIXES)
    }
    missing = checkpoint_names - set(model.state_dict())
    if missing:
        sample = ", ".join(sorted(missing)[:3])
        raise RuntimeError(
            f"{type(model).__name__} does not expose {len(missing)} of the "
            f"checkpoint's weights (e.g. {sample}). The rollout engine would leave "
            "them on the meta device. Load the checkpoint's own architecture, or add "
            "the prefix to IGNORED_CHECKPOINT_PREFIXES if the engine ignores it too."
        )


def chunked_logprobs(
    lm_head: "torch.nn.Module",
    hidden: "torch.Tensor",
    input_ids: "torch.Tensor",
    *,
    chunk_size: int,
) -> tuple["torch.Tensor", "torch.Tensor"]:
    """Next-token logprobs and per-token entropy, projected to vocab in time chunks.

    Returns two `[batch, seq-1]` tensors: the logprob the model assigns to the token
    actually generated at each position, and the entropy of the distribution there.

    The naive form (project everything, then log_softmax) needs several `[batch,
    seq, vocab]` fp32 tensors at once. At Qwen's ~250k vocab that is ~1 MB per token
    per tensor, so a long trajectory OOMs any GPU. Here each chunk's distribution is
    built, reduced to the two small outputs, and dropped; the chunk is wrapped in a
    checkpoint so backward recomputes it rather than keeping it alive. Peak vocab
    memory becomes `chunk_size x vocab` instead of `seq x vocab`.
    """
    import torch
    import torch.nn.functional as F
    from torch.utils.checkpoint import checkpoint

    def project(hidden_chunk: torch.Tensor, labels_chunk: torch.Tensor):
        logits = lm_head(hidden_chunk).float()
        log_probs = F.log_softmax(logits, dim=-1)
        gathered = log_probs.gather(-1, labels_chunk.unsqueeze(-1)).squeeze(-1)
        entropy = -(log_probs.exp() * log_probs).sum(dim=-1)
        return gathered, entropy

    # Position i predicts token i+1, so the last position has no label.
    shifted_hidden = hidden[:, :-1, :]
    labels = input_ids[:, 1:]

    gathered_chunks = []
    entropy_chunks = []
    for start in range(0, shifted_hidden.shape[1], chunk_size):
        stop = start + chunk_size
        hidden_chunk = shifted_hidden[:, start:stop, :]
        labels_chunk = labels[:, start:stop]
        if torch.is_grad_enabled() and hidden_chunk.requires_grad:
            gathered, entropy = checkpoint(
                project, hidden_chunk, labels_chunk, use_reentrant=False
            )
        else:
            gathered, entropy = project(hidden_chunk, labels_chunk)
        gathered_chunks.append(gathered)
        entropy_chunks.append(entropy.detach())

    return torch.cat(gathered_chunks, dim=1), torch.cat(entropy_chunks, dim=1)


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
        from transformers import AutoTokenizer

        from training.checkpoints import BackgroundCheckpointUploader

        model_path = cfg.resolved_model_path()

        self.beta = cfg.grpo.beta
        self.epsilon = cfg.grpo.epsilon
        self.loss_scale_factor = cfg.grpo.loss_scale_factor
        learning_rate = cfg.grpo.learning_rate
        self.min_rollouts_per_group = cfg.grpo.min_rollouts_per_group
        self.micro_batch_size = cfg.trainer.micro_batch_size
        self.logprob_chunk_size = cfg.trainer.logprob_chunk_size
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

        self.model = load_policy_model(model_path)
        self.model.train()
        if cfg.trainer.gradient_checkpointing:
            # Long trajectories make stored per-layer activations the second
            # largest consumer after the vocab projection; recompute them instead.
            self.model.config.use_cache = False
            self.model.gradient_checkpointing_enable()

        # Freeze everything outside the language stack. A vision tower rides along
        # so the state dict stays checkpoint-shaped for the rollout engine, but it
        # sees no text gradients and would only cost optimizer memory.
        self.trainable_params = []
        for name, param in self.model.named_parameters():
            if name.startswith(LANGUAGE_PARAM_PREFIXES):
                self.trainable_params.append(param)
            else:
                param.requires_grad_(False)

        self.optimizer = torch.optim.AdamW(self.trainable_params, lr=learning_rate)
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
            self.optimizer.zero_grad(set_to_none=True)
            tensors = self._collate_rollouts(rollouts, advantages)
            with record_duration("grl.train.step.duration"):
                loss, stats, mean_entropy = self._accumulate_gradients(tensors)
                grad_norm = torch.nn.utils.clip_grad_norm_(
                    self.trainable_params, float("inf")
                )
                self.optimizer.step()

            self.policy_version = max(self.policy_version, batch.policy_version) + 1
            self._record_train_metrics(
                loss=loss,
                stats=stats,
                grad_norm=float(grad_norm),
                mean_entropy=mean_entropy,
                tensors=tensors,
                advantages=advantages,
                rewards=rewards,
                num_rollouts=len(rollouts),
            )
            current.set_attribute("loss", loss)

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

    def _accumulate_gradients(
        self,
        tensors: dict[str, "torch.Tensor"],
    ) -> tuple[float, dict[str, float], float]:
        """Backward the batch in micro-batches, leaving accumulated grads on the model.

        Each micro-batch is normalized by the *whole batch's* denominator, so the
        summed gradients equal the gradient of the whole-batch loss: the objective
        is unchanged, only the peak memory is. Telemetry is likewise accumulated as
        mask-weighted sums and averaged over the full batch at the end.
        """
        mask = tensors["response_mask"]
        batch_size = int(mask.shape[0])
        # Padded response width of the *whole* batch, matching the pre-micro-batch
        # normalizer; a per-micro-batch width would silently reweight the loss.
        loss_scale = self.loss_scale_factor or int(mask.shape[1])
        denom = float(batch_size * loss_scale)

        total_loss = 0.0
        stat_sums: dict[str, float] = {}
        entropy_sum = 0.0
        token_count = 0

        for start in range(0, batch_size, self.micro_batch_size):
            stop = min(start + self.micro_batch_size, batch_size)
            micro = {name: tensor[start:stop] for name, tensor in tensors.items()}
            # Trim right padding to this micro-batch's longest sequence: the batch is
            # padded to its global longest, and carrying that into every forward wastes
            # the memory this loop exists to save.
            width = int(micro["attention_mask"].sum(dim=1).max().item())
            trainer_logprobs, micro_entropy_sum, micro_tokens = self._forward_logprobs(
                micro["input_ids"][:, :width],
                micro["attention_mask"][:, :width],
                micro["prompt_lens"],
                micro["response_lens"],
                max_resp=int(mask.shape[1]),
            )
            loss, sums = self._compute_loss(
                trainer_logprobs=trainer_logprobs,
                inference_logprobs=micro["inference_logprobs"],
                advantages=micro["advantages"],
                mask=micro["response_mask"],
                denom=denom,
            )
            loss.backward()

            total_loss += float(loss.item())
            for key, value in sums.items():
                stat_sums[key] = stat_sums.get(key, 0.0) + value
            entropy_sum += micro_entropy_sum
            token_count += micro_tokens

        tokens = float(stat_sums.pop("tokens", 0.0)) or 1.0
        stats = {key: value / tokens for key, value in stat_sums.items()}
        mean_entropy = entropy_sum / token_count if token_count else 0.0
        return total_loss, stats, mean_entropy

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

    def _forward_logprobs(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        prompt_lens: torch.Tensor,
        response_lens: torch.Tensor,
        *,
        max_resp: int,
    ) -> tuple["torch.Tensor", float, int]:
        """Response-token logprobs for one micro-batch, plus its entropy sum/count.

        Runs the backbone and the vocab projection separately so the projection can
        be chunked; calling the full causal-LM head in one shot would materialize
        the `[batch, seq, vocab]` tensor this whole path exists to avoid.

        ``max_resp`` is the whole batch's padded response width, so the returned
        rows line up with the caller's mask and inference-logprob slices.
        """
        import torch

        decoder = self.model.get_decoder()
        lm_head = self.model.get_output_embeddings()
        if decoder is None or lm_head is None:
            raise RuntimeError(
                f"{type(self.model).__name__} exposes no decoder/output embeddings; "
                "cannot compute logprobs without materializing full-vocab logits"
            )
        hidden = decoder(
            input_ids=input_ids, attention_mask=attention_mask
        ).last_hidden_state

        gathered, entropy = chunked_logprobs(
            lm_head,
            hidden,
            input_ids,
            chunk_size=self.logprob_chunk_size,
        )

        batch_size = int(input_ids.shape[0])
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
                entropy_sum += float(entropy[i, start:end].sum().item())
                token_count += resp_len

        return trainer_logprobs, entropy_sum, token_count

    def _compute_loss(
        self,
        trainer_logprobs: torch.Tensor,
        inference_logprobs: torch.Tensor,
        advantages: torch.Tensor,
        mask: torch.Tensor,
        *,
        denom: float,
    ) -> tuple[torch.Tensor, dict[str, float]]:
        """
        DrGRPO clipped policy loss with KL against rollout-time logprobs
        (prime-rl inference_logprobs path; beta is always applied).

        Token losses are summed and normalized by batch size times a response
        length constant, avoiding per-rollout token-mean length bias. When
        ``loss_scale_factor`` is unset, use the current padded response width.

        ``denom`` is that normalizer for the *whole* batch and is supplied by the
        caller, so a micro-batch contributes its true share of the gradient rather
        than being rescaled to its own size.

        Returns the differentiable loss plus unnormalized, mask-weighted telemetry
        sums (policy-gradient term, KL, ratio, clip count, token count) computed from
        the same tensors, so observability adds no extra forward pass. The caller
        divides the sums by the batch's total token count.
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
        loss = masked.sum() / denom
        sums = self._loss_stat_sums(per_token_pg, per_token_kl, ratio, mask)
        return loss, sums

    def _loss_stat_sums(
        self,
        per_token_pg: torch.Tensor,
        per_token_kl: torch.Tensor,
        ratio: torch.Tensor,
        mask: torch.Tensor,
    ) -> dict[str, float]:
        """Mask-weighted sums of the loss components, to be averaged across micro-batches."""
        import torch

        with torch.no_grad():
            mask_f = mask.to(ratio.dtype)
            clipped = (
                (ratio < 1 - self.epsilon) | (ratio > 1 + self.epsilon)
            ) & mask.bool()
            return {
                "pg_loss": float((per_token_pg * mask_f).sum().item()),
                "kl": float((per_token_kl * mask_f).sum().item()),
                "ratio_mean": float((ratio * mask_f).sum().item()),
                "clip_fraction": float(clipped.to(ratio.dtype).sum().item()),
                "tokens": float(mask_f.sum().item()),
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
