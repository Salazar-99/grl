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
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterator

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

logger = logging.getLogger(__name__)

_INITIALIZED = False


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
    global _INITIALIZED
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

    # Guaranteed delivery: BatchSpanProcessor and PeriodicExportingMetricReader
    # buffer in memory and would drop whatever is unflushed when the process
    # exits. shutdown() force-flushes both before stopping their exporters.
    # atexit fires on graceful interpreter shutdown, including Ray actor
    # teardown; the collector's own sending_queue + retry covers the rest.
    atexit.register(_shutdown, tracer_provider, meter_provider)

    _INITIALIZED = True
    logger.info(
        "telemetry initialized: role=%s run_id=%s endpoint=%s",
        role,
        run_id or "<unset>",
        endpoint,
    )


def _shutdown(tracer_provider: TracerProvider, meter_provider: MeterProvider) -> None:
    tracer_provider.shutdown()
    meter_provider.shutdown()


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
