//! OpenTelemetry metrics for the environment manager.
//!
//! Mirrors `training/telemetry.py`: a process-wide OTLP meter provider that
//! pushes to the in-cluster `grl-collector`. When `OTEL_EXPORTER_OTLP_ENDPOINT`
//! is unset the OTel global stays the default no-op provider, so every helper
//! here is safe to call regardless — disabled runs simply discard the data.
//!
//! The manager is a long-lived, environment-scoped DaemonSet, so its metrics
//! carry `service.name=grl-manager` (plus `env.id`/`pod`) but no `run.id`; the
//! dashboard scopes them to a run by time window, like the scraped infra jobs.

use std::time::Instant;

use opentelemetry::global;
use opentelemetry::metrics::{Counter, Gauge, Histogram, Meter};
use opentelemetry::KeyValue;
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::{runtime, Resource};
use opentelemetry_semantic_conventions::resource::SERVICE_NAME;
use tonic::Status;

const METER_NAME: &str = "grl.manager";

/// Holds the provider so the periodic reader keeps flushing for the process
/// lifetime; `shutdown()` on drop force-flushes whatever is still buffered.
pub struct TelemetryGuard {
    provider: SdkMeterProvider,
}

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        let _ = self.provider.shutdown();
    }
}

/// Wire up an OTLP/gRPC meter provider for this process. Returns `None` (a
/// no-op, matching the Python disabled path) when no endpoint is configured.
pub fn init_telemetry(role: &str) -> Option<TelemetryGuard> {
    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok()?;
    if endpoint.trim().is_empty() {
        return None;
    }

    let exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()
        .map_err(|err| eprintln!("otel metric exporter init failed: {err}"))
        .ok()?;

    let reader = PeriodicReader::builder(exporter, runtime::Tokio).build();

    let resource = Resource::new(vec![
        KeyValue::new(SERVICE_NAME, "grl-manager"),
        KeyValue::new("grl.role", role.to_string()),
        KeyValue::new("env.id", std::env::var("GRL_ENV_ID").unwrap_or_default()),
        KeyValue::new("pod", std::env::var("HOSTNAME").unwrap_or_default()),
    ]);

    let provider = SdkMeterProvider::builder()
        .with_reader(reader)
        .with_resource(resource)
        .build();

    global::set_meter_provider(provider.clone());
    Some(TelemetryGuard { provider })
}

/// The global meter. When telemetry is disabled this is the OTel no-op meter,
/// so instruments built from it record nothing.
pub fn meter() -> Meter {
    global::meter(METER_NAME)
}

pub fn counter(name: &'static str) -> Counter<u64> {
    meter().u64_counter(name).build()
}

pub fn histogram(name: &'static str) -> Histogram<f64> {
    meter().f64_histogram(name).build()
}

pub fn gauge(name: &'static str) -> Gauge<f64> {
    meter().f64_gauge(name).build()
}

/// Record one finished RPC: a latency histogram plus a counter tagged with the
/// resulting gRPC status. Call with the handler's `Result` before returning so
/// both success and error codes are captured.
pub fn record_rpc<T>(rpc: &'static str, start: Instant, result: &Result<T, Status>) {
    let code = match result {
        Ok(_) => tonic::Code::Ok,
        Err(status) => status.code(),
    };
    histogram("grl.manager.rpc.duration")
        .record(start.elapsed().as_secs_f64(), &[KeyValue::new("rpc", rpc)]);
    counter("grl.manager.rpc.requests").add(
        1,
        &[
            KeyValue::new("rpc", rpc),
            KeyValue::new("code", format!("{code:?}")),
        ],
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_telemetry_is_disabled_without_endpoint() {
        // No endpoint configured -> no provider, mirroring the Python no-op path.
        unsafe {
            std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        }
        assert!(init_telemetry("manager").is_none());

        // Helpers built off the (no-op) global meter must still be callable.
        counter("grl.test.counter").add(1, &[]);
        histogram("grl.test.hist").record(0.1, &[]);
        gauge("grl.test.gauge").record(1.0, &[]);
    }
}
