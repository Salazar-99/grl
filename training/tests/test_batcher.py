"""Tests for GRPO batcher group assembly timeouts."""

from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any
from unittest.mock import patch

from training.main import (
    GRPOGroupRequest,
    _flush_expired_groups,
    _pad_group_timeouts,
    trainer_loop,
)
from training.main import rollout_loop
from training.types import RolloutResult, TrainingBatch


def _rollout(*, group_id: str, index: int, expected: int = 2) -> RolloutResult:
    return RolloutResult(
        group_id=group_id,
        task_id="t1",
        env_id=f"env-{index}",
        rollout_index=index,
        expected_group_size=expected,
        policy_version_current=0,
        request_id=f"req-{index}",
        prompt_ids=[1],
        response_ids=[2],
        response_mask=[1],
        inference_logprobs=[0.0],
        num_turns=1,
        reward=1.0,
        done_reason="completed",
    )


class GroupAssemblyTests(unittest.TestCase):
    def test_pad_group_timeouts_fills_missing_rollouts(self) -> None:
        members = [_rollout(group_id="g1", index=0)]
        padded = _pad_group_timeouts(members)
        self.assertEqual(len(padded), 2)
        missing = next(r for r in padded if r.rollout_index == 1)
        self.assertEqual(missing.done_reason, "infra_error")
        self.assertIsNone(missing.reward)

    def test_flush_expired_groups_emits_partial_group(self) -> None:
        partial = {"g1": [_rollout(group_id="g1", index=0)]}
        started = {"g1": time.monotonic() - 100.0}
        expired = _flush_expired_groups(
            partial,
            started,
            group_assembly_timeout_secs=10.0,
        )
        self.assertEqual(len(expired), 1)
        self.assertEqual(len(expired[0]), 2)
        self.assertNotIn("g1", partial)
        self.assertNotIn("g1", started)


class RolloutLoopSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollout_task_creation_is_bounded_by_in_flight_limit(self) -> None:
        class RemoteMethod:
            def __init__(self, name: str) -> None:
                self.name = name

            def remote(self, *args: Any) -> tuple[str, tuple[Any, ...]]:
                return self.name, args

        class FakeWorker:
            def __init__(self) -> None:
                self.run_rollout = RemoteMethod("rollout")
                self.get_policy_version = RemoteMethod("policy")

        pending_tasks: asyncio.Queue[GRPOGroupRequest] = asyncio.Queue()
        completed_rollouts: asyncio.Queue[RolloutResult] = asyncio.Queue()
        await pending_tasks.put(GRPOGroupRequest(task_id="t1", num_rollouts=5))

        started_rollouts = 0
        first_wave_started = asyncio.Event()
        release_rollouts = asyncio.Event()

        async def fake_get(ref: tuple[str, tuple[Any, ...]]) -> Any:
            nonlocal started_rollouts
            kind, args = ref
            if kind == "policy":
                return 0

            started_rollouts += 1
            request = args[0]
            if started_rollouts == 2:
                first_wave_started.set()
            await release_rollouts.wait()
            return _rollout(
                group_id=request.group_id,
                index=request.rollout_index,
                expected=request.expected_group_size,
            )

        original_create_task = asyncio.create_task
        created_rollout_tasks = 0

        def recording_create_task(coro: Any) -> asyncio.Task[Any]:
            nonlocal created_rollout_tasks
            created_rollout_tasks += 1
            return original_create_task(coro)

        with (
            patch("training.main._get", side_effect=fake_get),
            patch("training.main.asyncio.create_task", side_effect=recording_create_task),
        ):
            loop_task = original_create_task(
                rollout_loop(
                    pending_tasks,
                    completed_rollouts,
                    [FakeWorker()],
                    max_in_flight=2,
                    trajectory_timeout_secs=60.0,
                )
            )
            try:
                await asyncio.wait_for(first_wave_started.wait(), timeout=1.0)
                await asyncio.sleep(0)
                self.assertEqual(created_rollout_tasks, 2)
                self.assertEqual(started_rollouts, 2)

                release_rollouts.set()
                for _ in range(5):
                    await asyncio.wait_for(completed_rollouts.get(), timeout=1.0)
                    completed_rollouts.task_done()
                self.assertEqual(created_rollout_tasks, 5)
            finally:
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task


class TrainerLoopTerminationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trainer_loop_stops_after_max_train_steps_and_checkpoints(self) -> None:
        class RemoteMethod:
            def __init__(self, name: str) -> None:
                self.name = name

            def remote(self, *args: Any) -> tuple[str, tuple[Any, ...]]:
                return self.name, args

        class FakeTrainingWorker:
            def __init__(self) -> None:
                self.train_batch = RemoteMethod("train")
                self.save_checkpoint = RemoteMethod("checkpoint")

        train_batches: asyncio.Queue[TrainingBatch] = asyncio.Queue()
        for index in range(2):
            await train_batches.put(
                TrainingBatch(
                    batch_id=f"batch-{index}",
                    groups=[[_rollout(group_id=f"g{index}", index=0, expected=1)]],
                    policy_version=index,
                )
            )

        policy_versions = iter([1, 2])

        async def fake_get(ref: tuple[str, tuple[Any, ...]]) -> Any:
            kind, _args = ref
            if kind == "train":
                return next(policy_versions)
            if kind == "checkpoint":
                return "/tmp/checkpoint"
            raise AssertionError(f"unexpected ref kind: {kind}")

        with patch("training.main._get", side_effect=fake_get):
            updates = await asyncio.wait_for(
                trainer_loop(
                    train_batches,
                    FakeTrainingWorker(),
                    [],
                    max_train_steps=2,
                ),
                timeout=1.0,
            )

        self.assertEqual(updates, 2)
        self.assertEqual(train_batches.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
