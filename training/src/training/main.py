import argparse
import asyncio
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import ray

from training.config import DEFAULT_CONFIG_PATH, GRLConfig
from training.environments import RpcTimeouts, list_task_ids
from training.rollouts import RolloutRequest, RolloutResult, RolloutWorker
from training.telemetry import init_telemetry
from training.trainer import TrainingBatch, TrainingWorker


@dataclass
class GRPOGroupRequest:
    # The environment renders the prompt and tools from this task_id, so the
    # group request only needs the task identity and sampling knobs.
    task_id: str
    num_rollouts: int = 8
    sampling_params: dict[str, Any] | None = None


async def _get(ref: ray.ObjectRef) -> Any:
    return await asyncio.to_thread(ray.get, ref)


def _timeout_rollout_result(template: RolloutResult, *, rollout_index: int) -> RolloutResult:
    return RolloutResult(
        group_id=template.group_id,
        task_id=template.task_id,
        env_id="",
        rollout_index=rollout_index,
        expected_group_size=template.expected_group_size,
        policy_version=template.policy_version,
        request_id="",
        prompt_ids=[],
        response_ids=[],
        response_mask=[],
        inference_logprobs=[],
        num_turns=0,
        reward=None,
        done_reason="infra_error",
    )


def _pad_group_timeouts(members: list[RolloutResult]) -> list[RolloutResult]:
    if not members:
        return members
    expected = members[0].expected_group_size
    present = {rollout.rollout_index for rollout in members}
    padded = list(members)
    template = members[0]
    for index in range(expected):
        if index not in present:
            padded.append(_timeout_rollout_result(template, rollout_index=index))
    return padded


async def rollout_loop(
    pending_tasks: asyncio.Queue[GRPOGroupRequest],
    completed_rollouts: asyncio.Queue[RolloutResult],
    rollout_workers: list[ray.actor.ActorHandle],
    *,
    max_in_flight: int,
    trajectory_timeout_secs: float,
) -> None:
    in_flight = asyncio.Semaphore(max_in_flight)
    next_worker = 0

    async def run_one(
        worker: ray.actor.ActorHandle,
        request: RolloutRequest,
    ) -> None:
        async with in_flight:
            try:
                result = await asyncio.wait_for(
                    _get(worker.run_rollout.remote(request)),
                    timeout=trajectory_timeout_secs,
                )
                await completed_rollouts.put(result)
            except asyncio.TimeoutError:
                await completed_rollouts.put(
                    RolloutResult(
                        group_id=request.group_id,
                        task_id=request.task_id,
                        env_id="",
                        rollout_index=request.rollout_index,
                        expected_group_size=request.expected_group_size,
                        policy_version=request.policy_version,
                        request_id="",
                        prompt_ids=[],
                        response_ids=[],
                        response_mask=[],
                        inference_logprobs=[],
                        num_turns=0,
                        reward=None,
                        done_reason="infra_error",
                    )
                )
            except Exception as exc:
                await completed_rollouts.put(
                    RolloutResult(
                        group_id=request.group_id,
                        task_id=request.task_id,
                        env_id="",
                        rollout_index=request.rollout_index,
                        expected_group_size=request.expected_group_size,
                        policy_version=request.policy_version,
                        request_id="",
                        prompt_ids=[],
                        response_ids=[],
                        response_mask=[],
                        inference_logprobs=[],
                        num_turns=0,
                        done_reason=f"error: {exc}",
                    )
                )

    while True:
        group = await pending_tasks.get()
        worker = rollout_workers[next_worker % len(rollout_workers)]
        next_worker += 1
        group_id = uuid.uuid4().hex
        policy_version = await _get(worker.get_policy_version.remote())

        for rollout_index in range(group.num_rollouts):
            request = RolloutRequest(
                group_id=group_id,
                task_id=group.task_id,
                rollout_index=rollout_index,
                expected_group_size=group.num_rollouts,
                policy_version=policy_version,
                sampling_params=group.sampling_params or {},
            )
            asyncio.create_task(run_one(worker, request))

        pending_tasks.task_done()


async def task_loop(
    pending_tasks: asyncio.Queue[GRPOGroupRequest],
    task_ids: list[str],
    *,
    num_rollouts: int,
    sampling_params: dict[str, Any],
    seed: int,
) -> None:
    """Stream GRPO groups, one per task, reshuffling each epoch.

    Reading the task set as a static index lets us shuffle deterministically and
    backpressure on ``pending_tasks`` (the queue's maxsize) instead of flooding
    the rollout workers.
    """
    if not task_ids:
        raise RuntimeError("no tasks to train on")
    rng = random.Random(seed)
    order = list(task_ids)
    while True:
        rng.shuffle(order)
        for task_id in order:
            await pending_tasks.put(
                GRPOGroupRequest(
                    task_id=task_id,
                    num_rollouts=num_rollouts,
                    sampling_params=sampling_params,
                )
            )


def _group_policy_version(group: list[RolloutResult]) -> int:
    return max(rollout.policy_version for rollout in group)


def _batch_size_to_emit(
    ready_groups: list[list[RolloutResult]],
    *,
    groups_per_batch: int,
    latest_policy_version: int,
    max_policy_staleness: int,
) -> int:
    if not ready_groups:
        return 0
    if len(ready_groups) >= groups_per_batch:
        return groups_per_batch
    oldest_version = _group_policy_version(ready_groups[0])
    if latest_policy_version - oldest_version > max_policy_staleness:
        return len(ready_groups)
    return 0


def _flush_expired_groups(
    partial_groups: dict[str, list[RolloutResult]],
    group_started: dict[str, float],
    *,
    group_assembly_timeout_secs: float,
) -> list[list[RolloutResult]]:
    now = time.monotonic()
    expired: list[list[RolloutResult]] = []
    for group_id, members in list(partial_groups.items()):
        if not members:
            continue
        started = group_started.get(group_id)
        if started is None:
            continue
        if len(members) >= members[0].expected_group_size:
            continue
        if now - started < group_assembly_timeout_secs:
            continue
        partial_groups.pop(group_id, None)
        group_started.pop(group_id, None)
        expired.append(_pad_group_timeouts(members))
    return expired


async def batcher_loop(
    completed_rollouts: asyncio.Queue[RolloutResult],
    train_batches: asyncio.Queue[TrainingBatch],
    *,
    groups_per_batch: int,
    max_policy_staleness: int,
    group_assembly_timeout_secs: float,
    group_poll_interval_secs: float,
) -> None:
    partial_groups: dict[str, list[RolloutResult]] = {}
    group_started: dict[str, float] = {}
    ready_groups: list[list[RolloutResult]] = []
    latest_policy_version = 0

    async def emit_ready_batches() -> None:
        nonlocal ready_groups
        while True:
            batch_size = _batch_size_to_emit(
                ready_groups,
                groups_per_batch=groups_per_batch,
                latest_policy_version=latest_policy_version,
                max_policy_staleness=max_policy_staleness,
            )
            if batch_size == 0:
                return
            batch_groups = ready_groups[:batch_size]
            del ready_groups[:batch_size]
            await train_batches.put(
                TrainingBatch(
                    batch_id=uuid.uuid4().hex,
                    groups=batch_groups,
                    policy_version=max(
                        _group_policy_version(group) for group in batch_groups
                    ),
                )
            )

    while True:
        try:
            result = await asyncio.wait_for(
                completed_rollouts.get(),
                timeout=group_poll_interval_secs,
            )
        except asyncio.TimeoutError:
            result = None

        if result is not None:
            latest_policy_version = max(latest_policy_version, result.policy_version)
            group = partial_groups.setdefault(result.group_id, [])
            if not group:
                group_started[result.group_id] = time.monotonic()
            group.append(result)

            if len(group) == result.expected_group_size:
                ready_groups.append(partial_groups.pop(result.group_id))
                group_started.pop(result.group_id, None)
                await emit_ready_batches()
            completed_rollouts.task_done()

        for expired_group in _flush_expired_groups(
            partial_groups,
            group_started,
            group_assembly_timeout_secs=group_assembly_timeout_secs,
        ):
            ready_groups.append(expired_group)
            await emit_ready_batches()


async def trainer_loop(
    train_batches: asyncio.Queue[TrainingBatch],
    training_workers: list[ray.actor.ActorHandle],
    rollout_workers: list[ray.actor.ActorHandle],
) -> None:
    next_worker = 0
    while True:
        batch = await train_batches.get()
        worker = training_workers[next_worker % len(training_workers)]
        next_worker += 1
        await _get(worker.train_batch.remote(batch, rollout_workers))
        train_batches.task_done()


async def run(config: GRLConfig, run_id: str) -> None:
    ray.init(ignore_reinit_error=config.ray.ignore_reinit_error)

    config_payload = config.model_dump()
    rollout_workers = [
        RolloutWorker.remote(config_payload, run_id=run_id)
        for _ in range(config.workers.num_rollout_workers)
    ]
    training_workers = [
        TrainingWorker.remote(config_payload, run_id=run_id)
        for _ in range(config.workers.num_training_workers)
    ]

    rpc_timeouts = RpcTimeouts.from_config(config.environment.rpc_timeouts)
    task_ids = await list_task_ids(
        addr=config.environment.server_addr,
        split=config.environment.split,
        rpc_timeouts=rpc_timeouts,
    )

    pipeline = config.pipeline
    pending_tasks: asyncio.Queue[GRPOGroupRequest] = asyncio.Queue(
        maxsize=pipeline.pending_tasks_queue_size
    )
    completed_rollouts: asyncio.Queue[RolloutResult] = asyncio.Queue(
        maxsize=pipeline.completed_rollouts_queue_size
    )
    train_batches: asyncio.Queue[TrainingBatch] = asyncio.Queue(
        maxsize=pipeline.train_batches_queue_size
    )

    grpo = config.grpo
    await asyncio.gather(
        task_loop(
            pending_tasks,
            task_ids,
            num_rollouts=grpo.num_rollouts,
            sampling_params=grpo.sampling_params(),
            seed=pipeline.seed,
        ),
        rollout_loop(
            pending_tasks,
            completed_rollouts,
            rollout_workers,
            max_in_flight=config.workers.max_in_flight_rollouts,
            trajectory_timeout_secs=config.rollout.trajectory_timeout_secs,
        ),
        batcher_loop(
            completed_rollouts,
            train_batches,
            groups_per_batch=grpo.groups_per_batch,
            max_policy_staleness=pipeline.max_policy_staleness,
            group_assembly_timeout_secs=pipeline.group_assembly_timeout_secs,
            group_poll_interval_secs=pipeline.group_poll_interval_secs,
        ),
        trainer_loop(train_batches, training_workers, rollout_workers),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GRL training")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the training YAML config",
    )
    args = parser.parse_args()

    config = GRLConfig.from_yaml(args.config)
    run_id = config.resolve_run_id()
    init_telemetry("head", run_id, otel_endpoint=config.telemetry.otel_endpoint)
    asyncio.run(run(config, run_id))


if __name__ == "__main__":
    main()
