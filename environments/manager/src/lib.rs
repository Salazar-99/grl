pub mod catalog;
pub mod environment;
pub mod registry;
pub mod reload;
pub mod telemetry;
pub mod vm;

pub mod pb {
    tonic::include_proto!("grl.environment.v1");
}
