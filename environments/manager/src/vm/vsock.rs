//! Host-side vsock dial to the guest executor.

use std::path::Path;
use std::time::Duration;

#[cfg(target_os = "linux")]
use super::executor::ExecutorConn;

#[cfg(target_os = "linux")]
pub async fn connect_executor(
    socket_path: &Path,
    timeout: Duration,
) -> Result<ExecutorConn, String> {
    use tokio::time::{Instant, sleep};

    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(format!(
                "Firecracker vsock connect {} timed out",
                socket_path.display()
            ));
        }
        // Keep each blocking Unix socket operation short. If the async boot task is
        // aborted, at most one bounded 200ms blocking worker remains.
        let attempt_timeout = remaining.min(Duration::from_millis(200));
        let socket_path = socket_path.to_path_buf();
        let result = tokio::task::spawn_blocking(move || {
            ExecutorConn::connect_firecracker_vsock(&socket_path, attempt_timeout)
        })
        .await
        .map_err(|err| format!("vsock connect task failed: {err}"))?;
        match result {
            Ok(connection) => return Ok(connection),
            Err(err) if Instant::now() >= deadline => return Err(err),
            Err(_) => sleep(Duration::from_millis(50)).await,
        }
    }
}

#[cfg(not(target_os = "linux"))]
pub async fn connect_executor(
    _socket_path: &Path,
    _timeout: Duration,
) -> Result<super::executor::ExecutorConn, String> {
    Err(format!(
        "vsock is only supported on Linux (executor port {})",
        super::config::EXECUTOR_VSOCK_PORT
    ))
}
