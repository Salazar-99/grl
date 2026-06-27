"""Manual OpenTelemetry instrumentation for the training package.

Every role (driver, ``TrainingWorker``, ``RolloutWorker``) is a separate
process — Ray actors run in their own interpreters — so ``init_telemetry`` must
be called once per process entrypoint, not once in the driver.

Exports go to the in-cluster collector (``grl-collector``) via OTLP/gRPC. The
endpoint comes from ``GRLConfig.telemetry.otel_endpoint`` (falling back to
``OTEL_EXPORTER_OTLP_ENDPOINT`` when unset). The in-cluster collector relays to
the external VM, which owns the ClickHouse export; this code never talks to the
external collector directly, so its HTTP/basic-auth receiver is not our concern.

Run identity (``run.id``) is resolved once in the driver from
``GRLConfig.telemetry.run_id`` (or generated fresh) and passed explicitly to each
Ray actor constructor, which forwards it to ``init_telemetry``. This keeps a run
scoped to one driver invocation against a long-lived cluster — no per-run infra
redeploy.

``run.id`` is set as a *Resource* attribute so every span/metric/log inherits it,
which is what the external pipeline's materialized view keys on (it drops rows
where ``run.id`` is empty).

When the endpoint is unset (local runs), the OTel API falls back to no-op
providers, so ``span()`` / ``counter()`` / ``histogram()`` stay safe to call.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Iterator, Sequence

from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber, set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Observation,
)
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

logger = logging.getLogger(__name__)

_INITIALIZED = False

# Held so ``log_trajectory`` can emit through a provider whose Resource carries
# ``run.id`` (the trajectory MV keys on it). ``None`` until ``init_telemetry``
# runs with an endpoint, which is also how ``log_trajectory`` knows it's disabled.
_LOGGER_PROVIDER: LoggerProvider | None = None


def new_run_id() -> str:
    """A fresh human-readable run id, e.g. ``grl-20260613-141503-9f3a1c``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"grl-{stamp}-{uuid.uuid4().hex[:6]}"


def init_telemetry(
    role: str,
    run_id: str = "",
    *,
    otel_endpoint: str | None = None,
) -> None:
    """Wire up trace/metric providers for this process. Idempotent.

    ``run_id`` and ``otel_endpoint`` are passed explicitly from the driver so
    every role in one run shares them. No-ops when no endpoint is configured.
    """
    global _INITIALIZED, _LOGGER_PROVIDER
    if _INITIALIZED:
        return

    endpoint = otel_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("otel endpoint unset; telemetry disabled")
        return

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint

    resource = Resource.create(
        {"service.name": f"grl-{role}", "grl.role": role, "run.id": run_id}
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)

    # Logs carry whole trajectories (see ``log_trajectory``); they ride the same
    # OTLP path and Resource so ``run.id`` is set, which the trajectory MV keys on.
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    _LOGGER_PROVIDER = logger_provider

    # Guaranteed delivery: the batch processors and PeriodicExportingMetricReader
    # buffer in memory and would drop whatever is unflushed when the process
    # exits. shutdown() force-flushes each before stopping their exporters.
    # atexit fires on graceful interpreter shutdown, including Ray actor
    # teardown; the collector's own sending_queue + retry covers the rest.
    atexit.register(_shutdown, tracer_provider, meter_provider, logger_provider)

    _INITIALIZED = True
    logger.info(
        "telemetry initialized: role=%s run_id=%s endpoint=%s",
        role,
        run_id or "<unset>",
        endpoint,
    )


def _shutdown(
    tracer_provider: TracerProvider,
    meter_provider: MeterProvider,
    logger_provider: LoggerProvider,
) -> None:
    tracer_provider.shutdown()
    meter_provider.shutdown()
    logger_provider.shutdown()


def _tracer() -> trace.Tracer:
    return trace.get_tracer("grl.training")


def _meter() -> metrics.Meter:
    return metrics.get_meter("grl.training")


@contextmanager
def span(name: str, **attributes: object) -> Iterator[Span]:
    """Start a current span with the given attributes. No-op when disabled."""
    with _tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current


@lru_cache(maxsize=None)
def counter(name: str, unit: str = "1", description: str = "") -> Counter:
    """A monotonic counter, cached so repeated call sites share one instrument."""
    return _meter().create_counter(name, unit=unit, description=description)


@lru_cache(maxsize=None)
def histogram(name: str, unit: str = "1", description: str = "") -> Histogram:
    """A value-distribution histogram, cached across call sites."""
    return _meter().create_histogram(name, unit=unit, description=description)


@lru_cache(maxsize=None)
def gauge(name: str, unit: str = "1", description: str = "") -> Any:
    """A synchronous gauge for last-value signals (loss, queue depth, …).

    Cached so repeated ``.set()`` call sites share one instrument.
    """
    return _meter().create_gauge(name, unit=unit, description=description)


_OBSERVABLE_NAMES: set[str] = set()


def observable_gauge(
    name: str,
    callback: Callable[[CallbackOptions], Sequence[Observation]],
    *,
    unit: str = "1",
    description: str = "",
) -> None:
    """Register an async gauge whose ``callback`` is polled on each export.

    Use for sampled state we don't want to push imperatively (queue depths,
    in-flight counts). Registering the same name twice is ignored so call sites
    in re-entrant code stay safe.
    """
    if name in _OBSERVABLE_NAMES:
        return
    _OBSERVABLE_NAMES.add(name)
    _meter().create_observable_gauge(
        name, callbacks=[callback], unit=unit, description=description
    )


@contextmanager
def record_duration(name: str, **attributes: Any) -> Iterator[None]:
    """Time the wrapped block and record seconds into histogram ``name``."""
    start = time.perf_counter()
    try:
        yield
    finally:
        histogram(name, unit="s").record(time.perf_counter() - start, attributes)


def log_trajectory(
    *,
    task_id: str,
    group_id: str,
    rollout_index: int,
    policy_version_start: int,
    policy_version_current: int,
    num_turns: int,
    reward: float | None,
    done_reason: str,
    prompt_tokens: int,
    response_tokens: int,
    prompt: str = "",
    response: str = "",
) -> None:
    """Emit one finished trajectory as an OTLP log record. No-op when disabled.

    Structured fields go into log attributes (the trajectory MV promotes them
    into typed columns) under the ``grl.record=trajectory`` marker; the full
    rendered prompt/response text rides in the body for the trajectory browser.
    ``run.id`` is inherited from the shared Resource.
    """
    if _LOGGER_PROVIDER is None:
        return

    attributes: dict[str, Any] = {
        "grl.record": "trajectory",
        "task_id": task_id,
        "group_id": group_id,
        "rollout_index": rollout_index,
        "policy_version_start": policy_version_start,
        "policy_version_current": policy_version_current,
        "num_turns": num_turns,
        "done_reason": done_reason,
        "prompt_tokens": prompt_tokens,
        "response_tokens": response_tokens,
    }
    if reward is not None:
        attributes["reward"] = reward

    body = json.dumps({"prompt": prompt, "response": response})
    now = time.time_ns()
    _LOGGER_PROVIDER.get_logger("grl.training").emit(
        timestamp=now,
        observed_timestamp=now,
        severity_number=SeverityNumber.INFO,
        severity_text="INFO",
        body=body,
        attributes=attributes,
    )
