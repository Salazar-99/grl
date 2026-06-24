use std::sync::Arc;

use manager::catalog::Catalog;
use manager::environment::EnvironmentServiceImpl;
use manager::pb::environment_service_server::EnvironmentServiceServer;
use tonic::transport::Server;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let listen_addr = std::env::var("GRL_ENV_SERVER_ADDR").unwrap_or_else(|_| "0.0.0.0:50051".into());
    let addr = listen_addr.parse()?;

    let catalog = Arc::new(Catalog::from_env()?);
    println!(
        "environment manager listening on {addr} ({} task(s) in catalog)",
        catalog.len()
    );

    let service = EnvironmentServiceImpl::new(catalog);
    Server::builder()
        .add_service(EnvironmentServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}
