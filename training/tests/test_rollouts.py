"""Tests for GRPO rollout filtering and tool-call parsing."""

from __future__ import annotations

import re
import unittest

from training.rollouts import RolloutResult, ToolCall

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


def parse_first_tool_call(text: str) -> list[ToolCall]:
    """Mirror RolloutWorker._parse_tool_calls first-call-only behavior."""
    calls: list[ToolCall] = []
    for match in _TOOL_CALL_RE.finditer(text):
        import json

        payload = json.loads(match.group(1))
        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)
        if name:
            calls.append(ToolCall(name=name, arguments=str(arguments)))
            break
    return calls


class ToolCallParseTests(unittest.TestCase):
    def test_takes_first_tool_call_only(self) -> None:
        text = (
            '<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>'
            '<tool_call>{"name":"submit","arguments":{}}</tool_call>'
        )
        calls = parse_first_tool_call(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "bash")


class GrpoFilterTests(unittest.TestCase):
    def _make_rollout(self, *, reward: float | None, done_reason: str) -> RolloutResult:
        return RolloutResult(
            group_id="g",
            task_id="t",
            env_id="e",
            rollout_index=0,
            expected_group_size=2,
            policy_version=0,
            request_id="r",
            prompt_ids=[1],
            response_ids=[2],
            response_mask=[1],
            inference_logprobs=[0.0],
            num_turns=1,
            reward=reward,
            done_reason=done_reason,
        )

    def test_infra_errors_excluded_from_group(self) -> None:
        from training.trainer import grpo_valid_rollouts

        group = [
            self._make_rollout(reward=1.0, done_reason="completed"),
            self._make_rollout(reward=0.0, done_reason="infra_error"),
        ]
        valid = grpo_valid_rollouts(group, min_rollouts_per_group=2)
        self.assertEqual(valid, [])

    def test_valid_group_keeps_non_infra_rollouts(self) -> None:
        from training.trainer import grpo_valid_rollouts

        group = [
            self._make_rollout(reward=1.0, done_reason="completed"),
            self._make_rollout(reward=0.0, done_reason="completed"),
        ]
        valid = grpo_valid_rollouts(group, min_rollouts_per_group=2)
        self.assertEqual(len(valid), 2)


if __name__ == "__main__":
    unittest.main()
