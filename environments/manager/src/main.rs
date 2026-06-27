use std::sync::Arc;
use std::time::Duration;

use manager::catalog::Catalog;
use manager::environment::EnvironmentServiceImpl;
use manager::pb::environment_service_server::EnvironmentServiceServer;
use manager::telemetry;
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

    let catalog = Arc::new(Catalog::from_env()?);
    println!(
        "environment manager listening on {addr} ({} task(s) in catalog, request timeout {}s)",
        catalog.len(),
        request_timeout_secs
    );
    telemetry::gauge("grl.manager.catalog.tasks").record(catalog.len() as f64, &[]);

    let service = EnvironmentServiceImpl::new(catalog);
    service.install_metrics();
    Server::builder()
        .layer(TimeoutLayer::new(Duration::from_secs(request_timeout_secs)))
        .add_service(EnvironmentServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}
