use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use manager::catalog::Catalog;
use manager::environment::EnvironmentServiceImpl;
use manager::pb::environment_service_server::EnvironmentServiceServer;
use manager::{reload, telemetry};
use tonic::transport::Server;
use tower::timeout::TimeoutLayer;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Held until the server stops so the metric reader keeps flushing; dropping
    // it force-flushes whatever is still buffered.
    let _telemetry = telemetry::init_telemetry("manager");

    let listen_addr = std::env::var("GRL_ENV_SERVER_ADDR").unwrap_or_else(|_| "0.0.0.0:50051".into());
    let addr = listen_addr.parse()?;

    let request_timeout_secs: u64 = std::env::var("GRL_GRPC_REQUEST_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(960);

    // The catalog is swappable so a newly synced bundle can be hot-reloaded
    // without restarting the manager (see `reload`). It starts empty when no
    // bundle is present yet — the manager still binds and serves.
    let tasks_file = std::env::var("GRL_TASKS_FILE").ok();
    let catalog = Arc::new(ArcSwap::from_pointee(Catalog::from_env()?));
    let initial_tasks = catalog.load().len();
    if initial_tasks == 0 {
        match &tasks_file {
            Some(path) => println!(
                "[WARN] no active bundle present (tasks.jsonl missing at {path}); serving empty \
                 catalog — CreateEnvironment returns not-found until a bundle is synced"
            ),
            None => println!("[WARN] GRL_TASKS_FILE unset; serving empty catalog"),
        }
    }
    println!(
        "environment manager listening on {addr} ({initial_tasks} task(s) in catalog, request timeout {request_timeout_secs}s)"
    );

    // Watch the bundle's `.ready` sentinel and hot-reload the catalog on change.
    if let Some(path) = tasks_file {
        let poll_secs: u64 = std::env::var("GRL_CATALOG_POLL_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5);
        reload::spawn_catalog_reloader(
            Arc::clone(&catalog),
            PathBuf::from(path),
            Duration::from_secs(poll_secs),
        );
    }

    let service = EnvironmentServiceImpl::new(catalog);
    service.install_metrics();
    Server::builder()
        .layer(TimeoutLayer::new(Duration::from_secs(request_timeout_secs)))
        .add_service(EnvironmentServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}
