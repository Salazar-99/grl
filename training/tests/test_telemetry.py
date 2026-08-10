"""Tests for the training.telemetry helpers.

Two regimes are covered: the disabled path (no OTLP endpoint -> every helper a
safe no-op) and the enabled path, where a local in-memory meter is patched in so
we can assert the instruments actually record without standing up a collector.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation

from training import telemetry


def _reset_instrument_caches() -> None:
    """Drop cached instruments so they rebind to the patched/global meter."""
    telemetry.counter.cache_clear()
    telemetry.histogram.cache_clear()
    telemetry.gauge.cache_clear()
    telemetry._OBSERVABLE_NAMES.clear()


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    names: set[str] = set()
    data = reader.get_metrics_data()
    if data is None:
        return names
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                names.add(metric.name)
    return names


class TelemetryDisabledTests(unittest.TestCase):
    """With no endpoint configured every helper must be a safe no-op."""

    def test_helpers_do_not_raise_when_disabled(self) -> None:
        _reset_instrument_caches()
        telemetry.counter("grl.test.disabled.counter").add(1, {"k": "v"})
        telemetry.gauge("grl.test.disabled.gauge").set(3)
        telemetry.histogram("grl.test.disabled.hist", unit="s").record(0.25)
        telemetry.observable_gauge(
            "grl.test.disabled.obs", lambda _options: [Observation(1)]
        )
        with telemetry.record_duration("grl.test.disabled.dur", rpc="create"):
            pass
        with telemetry.span("grl.test.disabled.span", attr=1):
            pass

    def test_log_trajectory_is_noop_without_logger_provider(self) -> None:
        # _LOGGER_PROVIDER is only populated by init_telemetry with an endpoint.
        self.assertIsNone(telemetry._LOGGER_PROVIDER)
        telemetry.log_trajectory(
            task_id="t",
            group_id="g",
            rollout_index=0,
            policy_version_start=0,
            policy_version_current=1,
            num_turns=2,
            reward=0.5,
            done_reason="completed",
            prompt_tokens=3,
            response_tokens=4,
            prompt="hi",
            response="yo",
        )

    def test_resource_attributes_include_kubernetes_identity_when_available(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GRL_POD_NAME": "grl-training-abc",
                "GRL_POD_UID": "pod-uid",
                "GRL_POD_NAMESPACE": "default",
                "GRL_NODE_NAME": "gpu-node-1",
            },
            clear=False,
        ):
            attributes = telemetry.resource_attributes("training", "run-123")

        self.assertEqual(attributes["service.name"], "grl-training")
        self.assertEqual(attributes["run.id"], "run-123")
        self.assertEqual(attributes["k8s.pod.name"], "grl-training-abc")
        self.assertEqual(attributes["k8s.pod.uid"], "pod-uid")
        self.assertEqual(attributes["k8s.namespace.name"], "default")
        self.assertEqual(attributes["k8s.node.name"], "gpu-node-1")


class TelemetryInstrumentTests(unittest.TestCase):
    """With a real (in-memory) meter the helpers emit the expected instruments."""

    def test_instruments_record_into_reader(self) -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("grl.test")
        try:
            with patch.object(telemetry, "_meter", lambda: meter):
                _reset_instrument_caches()
                telemetry.counter("grl.test.counter").add(2, {"k": "v"})
                telemetry.gauge("grl.test.gauge").set(7)
                telemetry.histogram("grl.test.hist", unit="s").record(1.5)
                with telemetry.record_duration("grl.test.dur"):
                    pass
                telemetry.observable_gauge(
                    "grl.test.obs", lambda _options: [Observation(11)]
                )
                names = _metric_names(reader)
        finally:
            provider.shutdown()

        for expected in (
            "grl.test.counter",
            "grl.test.gauge",
            "grl.test.hist",
            "grl.test.dur",
            "grl.test.obs",
        ):
            self.assertIn(expected, names)


class TelemetryHistogramViewTests(unittest.TestCase):
    def test_rpc_duration_views_use_10ms_buckets_through_two_seconds(self) -> None:
        views = telemetry._rpc_duration_views()
        self.assertEqual(len(views), 1)
        self.assertEqual(telemetry.RPC_DURATION_BUCKETS[0], 0.0)
        self.assertEqual(telemetry.RPC_DURATION_BUCKETS[1], 0.01)
        self.assertEqual(telemetry.RPC_DURATION_BUCKETS[-1], 2.0)
        self.assertEqual(len(telemetry.RPC_DURATION_BUCKETS), 201)
        for view in views:
            aggregation = view._aggregation
            self.assertIsInstance(aggregation, ExplicitBucketHistogramAggregation)
            self.assertEqual(tuple(aggregation._boundaries), telemetry.RPC_DURATION_BUCKETS)


if __name__ == "__main__":
    unittest.main()
