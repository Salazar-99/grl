import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import ray

from training.environments import EnvironmentWorker
from training.rollouts import RolloutRequest, RolloutResult, RolloutWorker
from training.trainer import TrainingBatch, TrainingWorker


@dataclass
class GRPOGroupRequest:
    task_id: str
    messages: list[dict[str, Any]]
    num_rollouts: int = 8
    sampling_params: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None


async def _get(ref: ray.ObjectRef) -> Any:
    return await asyncio.to_thread(ray.get, ref)


async def rollout_submitter_loop(
    pending_tasks: asyncio.Queue[GRPOGroupRequest],
    completed_rollouts: asyncio.Queue[RolloutResult],
    rollout_worker: ray.actor.ActorHandle,
    *,
    max_in_flight: int,
) -> None:
    in_flight = asyncio.Semaphore(max_in_flight)

    async def run_one(request: RolloutRequest) -> None:
        async with in_flight:
            try:
                result = await _get(rollout_worker.run_rollout.remote(request))
                await completed_rollouts.put(result)
            except Exception as exc:
                await completed_rollouts.put(
                    RolloutResult(
                        group_id=request.group_id,
                        task_id=request.task_id,
                        env_id=request.env_id,
                        rollout_index=request.rollout_index,
                        expected_group_size=request.expected_group_size,
                        policy_version=request.policy_version,
                        request_id="",
                        prompt_ids=[],
                        response_ids=[],
                        response_mask=[],
                        num_turns=0,
                        done_reason=f"error: {exc}",
                    )
                )
            finally:
                await _get(request.env_actor.reset.remote())

    while True:
        group = await pending_tasks.get()
        group_id = uuid.uuid4().hex
        policy_version = await _get(rollout_worker.get_policy_version.remote())

        for rollout_index in range(group.num_rollouts):
            env_id = f"{group.task_id}:{group_id}:{rollout_index}"
            env_actor = EnvironmentWorker.remote(group.task_id, env_id)
            request = RolloutRequest(
                group_id=group_id,
                task_id=group.task_id,
                env_id=env_id,
                env_actor=env_actor,
                messages=group.messages,
                rollout_index=rollout_index,
                expected_group_size=group.num_rollouts,
                policy_version=policy_version,
                sampling_params=group.sampling_params or {},
                tools=group.tools,
            )
            asyncio.create_task(run_one(request))

        pending_tasks.task_done()


async def batcher_loop(
    completed_rollouts: asyncio.Queue[RolloutResult],
    train_batches: asyncio.Queue[TrainingBatch],
    *,
    groups_per_batch: int,
) -> None:
    partial_groups: dict[str, list[RolloutResult]] = {}
    ready_groups: dict[int, list[list[RolloutResult]]] = {}

    while True:
        result = await completed_rollouts.get()
        group = partial_groups.setdefault(result.group_id, [])
        group.append(result)

        if len(group) == result.expected_group_size:
            complete_group = partial_groups.pop(result.group_id)
            policy_groups = ready_groups.setdefault(result.policy_version, [])
            policy_groups.append(complete_group)

            if len(policy_groups) >= groups_per_batch:
                batch_groups = policy_groups[:groups_per_batch]
                del policy_groups[:groups_per_batch]
                await train_batches.put(
                    TrainingBatch(
                        batch_id=uuid.uuid4().hex,
                        groups=batch_groups,
                        policy_version=result.policy_version,
                    )
                )

        completed_rollouts.task_done()


async def trainer_loop(
    train_batches: asyncio.Queue[TrainingBatch],
    training_worker: ray.actor.ActorHandle,
    rollout_workers: list[ray.actor.ActorHandle],
) -> None:
    while True:
        batch = await train_batches.get()
        await _get(training_worker.train_batch.remote(batch, rollout_workers))
        train_batches.task_done()


async def run() -> None:
    ray.init(ignore_reinit_error=True)

    rollout_worker = RolloutWorker.remote()
    training_worker = TrainingWorker.remote()

    pending_tasks: asyncio.Queue[GRPOGroupRequest] = asyncio.Queue(maxsize=64)
    completed_rollouts: asyncio.Queue[RolloutResult] = asyncio.Queue(maxsize=256)
    train_batches: asyncio.Queue[TrainingBatch] = asyncio.Queue(maxsize=16)

    await asyncio.gather(
        rollout_submitter_loop(
            pending_tasks,
            completed_rollouts,
            rollout_worker,
            max_in_flight=32,
        ),
        batcher_loop(completed_rollouts, train_batches, groups_per_batch=4),
        trainer_loop(train_batches, training_worker, [rollout_worker]),
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
