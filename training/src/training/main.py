import argparse
import asyncio
import random
import uuid
from dataclasses import dataclass
from typing import Any

import ray

from training.config import DEFAULT_CONFIG_PATH, GRLConfig
from training.environments import list_task_ids
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


async def rollout_loop(
    pending_tasks: asyncio.Queue[GRPOGroupRequest],
    completed_rollouts: asyncio.Queue[RolloutResult],
    rollout_workers: list[ray.actor.ActorHandle],
    *,
    max_in_flight: int,
) -> None:
    in_flight = asyncio.Semaphore(max_in_flight)
    next_worker = 0

    async def run_one(
        worker: ray.actor.ActorHandle,
        request: RolloutRequest,
    ) -> None:
        async with in_flight:
            try:
                result = await _get(worker.run_rollout.remote(request))
                await completed_rollouts.put(result)
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


async def batcher_loop(
    completed_rollouts: asyncio.Queue[RolloutResult],
    train_batches: asyncio.Queue[TrainingBatch],
    *,
    groups_per_batch: int,
    max_policy_staleness: int,
) -> None:
    partial_groups: dict[str, list[RolloutResult]] = {}
    ready_groups: list[list[RolloutResult]] = []
    latest_policy_version = 0

    while True:
        result = await completed_rollouts.get()
        latest_policy_version = max(latest_policy_version, result.policy_version)
        group = partial_groups.setdefault(result.group_id, [])
        group.append(result)

        if len(group) == result.expected_group_size:
            ready_groups.append(partial_groups.pop(result.group_id))

            while True:
                batch_size = _batch_size_to_emit(
                    ready_groups,
                    groups_per_batch=groups_per_batch,
                    latest_policy_version=latest_policy_version,
                    max_policy_staleness=max_policy_staleness,
                )
                if batch_size == 0:
                    break
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

        completed_rollouts.task_done()


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

    # Task ids from the manager catalog; prompts/tools on CreateEnvironment.
    task_ids = await list_task_ids(
        addr=config.environment.server_addr,
        split=config.environment.split,
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
        ),
        batcher_loop(
            completed_rollouts,
            train_batches,
            groups_per_batch=grpo.groups_per_batch,
            max_policy_staleness=pipeline.max_policy_staleness,
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
