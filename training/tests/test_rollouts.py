"""Tests for GRPO rollout filtering and renderer helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from renderers import ParsedResponse

from training.rollouts import (
    GenerationResult,
    PolicyWeightsRef,
    Renderer,
    RolloutResult,
    RolloutWorker,
    Session,
)


class ToolCallParseTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch("training.rollouts._create_base_renderer"):
            self.renderer = Renderer(MagicMock(), "org/unmapped-model")

    def test_parsed_tool_calls_take_first_only(self) -> None:
        parsed = ParsedResponse(
            content="",
            tool_calls=[
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                },
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "submit", "arguments": "{}"},
                },
            ],
        )
        calls = self.renderer.parsed_tool_calls(parsed)
        self.assertEqual(calls, [("bash", '{"command":"ls"}')])

    def test_to_renderer_tools_unwraps_openai_schema(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run bash",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        specs = self.renderer.to_tools(tools)
        assert specs is not None
        self.assertEqual(specs[0]["name"], "bash")
        self.assertEqual(specs[0]["description"], "Run bash")


class GrpoFilterTests(unittest.TestCase):
    def _make_rollout(self, *, reward: float | None, done_reason: str) -> RolloutResult:
        return RolloutResult(
            group_id="g",
            task_id="t",
            env_id="e",
            rollout_index=0,
            expected_group_size=2,
            policy_version_current=0,
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


class PolicyUpdateTests(unittest.IsolatedAsyncioTestCase):
    def test_send_weights_puts_cpu_state_dict_in_object_store(self) -> None:
        import torch

        from training.trainer import TrainingWorker

        worker_cls = TrainingWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        worker.model = torch.nn.Linear(2, 1)
        original = worker.model.state_dict()["weight"]

        with patch("training.trainer.ray.put", return_value="ref") as ray_put:
            weights_ref = worker.send_weights()

        ray_put.assert_called_once()
        state_dict = ray_put.call_args.args[0]
        self.assertEqual(weights_ref, PolicyWeightsRef(ref="ref"))
        self.assertEqual(state_dict["weight"].device.type, "cpu")
        self.assertEqual(state_dict["bias"].device.type, "cpu")
        self.assertNotEqual(state_dict["weight"].data_ptr(), original.data_ptr())

    async def test_apply_policy_update_loads_weights_from_object_ref(self) -> None:
        import torch

        class FakeEngine:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            async def pause_generation(self, **kwargs: object) -> None:
                self.calls.append(("pause", kwargs))

            async def collective_rpc(
                self,
                method: str,
                *,
                kwargs: dict[str, object] | None = None,
            ) -> None:
                self.calls.append(("collective_rpc", (method, kwargs)))

            async def resume_generation(self) -> None:
                self.calls.append(("resume", {}))

        worker_cls = RolloutWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        worker.engine = FakeEngine()
        worker.policy_version = 0
        state_dict = {"model.embed.weight": torch.ones(1)}
        weights_ref = PolicyWeightsRef(ref=object())

        with patch("training.rollouts.ray.get", return_value=state_dict) as ray_get:
            await worker.apply_policy_update(1, weights_ref)

        ray_get.assert_called_once_with(weights_ref.ref)
        self.assertEqual(worker.policy_version, 1)
        self.assertEqual(
            worker.engine.calls[0],
            ("pause", {"mode": "keep", "clear_cache": True}),
        )
        method, kwargs = worker.engine.calls[1][1]
        self.assertEqual(method, "reload_weights")
        self.assertEqual(kwargs, {"weights_iterator": list(state_dict.items())})
        self.assertEqual(worker.engine.calls[2], ("resume", {}))

    async def test_trajectory_records_actual_policy_version_span(self) -> None:
        class FakeRenderer:
            stop_token_ids: list[int] = []

            def parse_tool_calls(self, token_ids: list[int]) -> list[tuple[str, str]]:
                return []

        worker_cls = RolloutWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        worker.policy_version = 2
        worker.max_assistant_turns = 4
        worker.generation_timeout_secs = 1.0
        worker.renderer = FakeRenderer()

        async def fake_generate_once(**kwargs: object) -> GenerationResult:
            worker.policy_version = 3
            return GenerationResult(token_ids=[11, 12], logprobs=[-0.1, -0.2])

        worker._generate_once = fake_generate_once
        session = Session(
            request_id="r",
            group_id="g",
            task_id="t",
            env=object(),
            rollout_index=0,
            expected_group_size=1,
            policy_version_start=0,
            policy_version_current=0,
            prompt_ids=[1],
        )

        result = await worker._run_trajectory(session, {}, None)

        self.assertEqual(result.policy_version_start, 2)
        self.assertEqual(result.policy_version_current, 3)
        self.assertEqual(result.response_ids, [11, 12])


class InstrumentationTests(unittest.TestCase):
    """Assert the instrumented hot paths actually emit their key instruments."""

    @staticmethod
    def _patched_meter():
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        return reader, provider, provider.get_meter("test")

    @staticmethod
    def _names(reader) -> set[str]:
        names: set[str] = set()
        data = reader.get_metrics_data()
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    names.add(metric.name)
        return names

    @staticmethod
    def _reset(telemetry) -> None:
        telemetry.counter.cache_clear()
        telemetry.histogram.cache_clear()
        telemetry.gauge.cache_clear()
        telemetry._OBSERVABLE_NAMES.clear()

    def test_record_rollout_metrics_emits_instruments(self) -> None:
        from training import telemetry

        reader, provider, meter = self._patched_meter()
        worker_cls = RolloutWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        result = RolloutResult(
            group_id="g",
            task_id="t",
            env_id="e",
            rollout_index=0,
            expected_group_size=1,
            policy_version_current=3,
            request_id="r",
            prompt_ids=[1, 2],
            response_ids=[3, 4, 5],
            response_mask=[1, 1, 1],
            inference_logprobs=[0.0, 0.0, 0.0],
            num_turns=2,
            reward=0.5,
            done_reason="completed",
            policy_version_start=1,
        )
        try:
            with patch.object(telemetry, "_meter", lambda: meter):
                self._reset(telemetry)
                worker._record_rollout_metrics(result, 1.5)
            names = self._names(reader)
        finally:
            provider.shutdown()

        self.assertIn("grl.rollout.completed", names)
        self.assertIn("grl.rollout.reward", names)
        self.assertIn("grl.rollout.duration", names)
        self.assertIn("grl.rollout.policy_staleness", names)

    def test_flatten_rollouts_counts_dropped_groups(self) -> None:
        from training import telemetry
        from training.trainer import TrainingWorker

        reader, provider, meter = self._patched_meter()
        worker_cls = TrainingWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        worker.min_rollouts_per_group = 2

        def make(reward: float | None, done_reason: str) -> RolloutResult:
            return RolloutResult(
                group_id="g",
                task_id="t",
                env_id="e",
                rollout_index=0,
                expected_group_size=2,
                policy_version_current=0,
                request_id="r",
                prompt_ids=[1],
                response_ids=[2],
                response_mask=[1],
                inference_logprobs=[0.0],
                num_turns=1,
                reward=reward,
                done_reason=done_reason,
            )

        group_all_infra = [make(None, "infra_error"), make(0.0, "infra_error")]
        try:
            with patch.object(telemetry, "_meter", lambda: meter):
                self._reset(telemetry)
                rollouts, advantages, rewards = worker._flatten_rollouts(
                    [group_all_infra]
                )
            names = self._names(reader)
        finally:
            provider.shutdown()

        self.assertEqual(rollouts, [])
        self.assertEqual(advantages, [])
        self.assertEqual(rewards, [])
        self.assertIn("grl.train.groups_dropped", names)


if __name__ == "__main__":
    unittest.main()
