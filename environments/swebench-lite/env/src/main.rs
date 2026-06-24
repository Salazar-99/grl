//! In-VM executor binary for the swebench-lite environment.

use std::sync::Arc;

use env::session::Sessions;

fn main() -> std::io::Result<()> {
    let default_listen = if cfg!(target_os = "linux") {
        "vsock:5005".to_string()
    } else {
        "127.0.0.1:5005".to_string()
    };
    let listen = std::env::var("GRL_EXECUTOR_LISTEN").unwrap_or(default_listen);

    let sessions = Arc::new(Sessions::default());
    env::server::serve(&listen, sessions)
}
