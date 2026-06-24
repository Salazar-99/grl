pub mod catalog;
pub mod environment;
pub mod registry;

pub mod pb {
    tonic::include_proto!("grl.environment.v1");
}
