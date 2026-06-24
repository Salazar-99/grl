//! In-VM executor library for the swebench-lite environment.

pub mod score;
pub mod server;
pub mod session;

/// Generated from `environments/proto/grl/environment/v1/environment.proto`.
pub mod pb {
    include!(concat!(env!("OUT_DIR"), "/grl.environment.v1.rs"));
}
