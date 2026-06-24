//! In-VM executor for the swebench-lite environment.
//!
//! Runs inside each Firecracker VM and implements the environment's tools. The
//! manager forwards tool calls over vsock; this process holds one long-lived
//! shell per environment ([`session`]) so state persists across calls, and
//! serves the framed protobuf protocol ([`server`]) the manager speaks.

mod score;
mod server;
mod session;

/// Generated from `environments/proto/grl/environment/v1/environment.proto` —
/// the same contract the manager and training client use.
mod pb {
    include!(concat!(env!("OUT_DIR"), "/grl.environment.v1.rs"));
}

use std::sync::Arc;

use session::Sessions;

fn main() -> std::io::Result<()> {
    // vsock in the VM; TCP off-VM (macOS dev, tests) where AF_VSOCK is absent.
    let default_listen = if cfg!(target_os = "linux") {
        "vsock:5005".to_string()
    } else {
        "127.0.0.1:5005".to_string()
    };
    let listen = std::env::var("GRL_EXECUTOR_LISTEN").unwrap_or(default_listen);

    let sessions = Arc::new(Sessions::default());
    server::serve(&listen, sessions)
}
