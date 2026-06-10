use grl_env_server::environment::EnvironmentServiceImpl;
use grl_env_server::pb::environment_service_server::EnvironmentServiceServer;
use tonic::transport::Server;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let listen_addr = std::env::var("GRL_ENV_SERVER_ADDR").unwrap_or_else(|_| "0.0.0.0:50051".into());
    let addr = listen_addr.parse()?;

    println!("environment server listening on {addr}");

    Server::builder()
        .add_service(EnvironmentServiceServer::new(EnvironmentServiceImpl))
        .serve(addr)
        .await?;

    Ok(())
}
