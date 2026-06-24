"""Tests for GRPO batcher group assembly timeouts."""

from __future__ import annotations

import time
import unittest

from training.main import _flush_expired_groups, _pad_group_timeouts
from training.rollouts import RolloutResult


def _rollout(*, group_id: str, index: int, expected: int = 2) -> RolloutResult:
    return RolloutResult(
        group_id=group_id,
        task_id="t1",
        env_id=f"env-{index}",
        rollout_index=index,
        expected_group_size=expected,
        policy_version=0,
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


if __name__ == "__main__":
    unittest.main()
