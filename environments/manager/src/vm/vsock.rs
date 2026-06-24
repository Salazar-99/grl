//! Host-side vsock dial to the guest executor.

use std::time::Duration;

#[cfg(target_os = "linux")]
use super::executor::ExecutorConn;

#[cfg(target_os = "linux")]
pub async fn wait_executor(guest_cid: u32, timeout: Duration) -> Result<(), String> {
    use tokio::time::{sleep, Instant};

    let deadline = Instant::now() + timeout;
    loop {
        match probe_executor(guest_cid) {
            Ok(()) => return Ok(()),
            Err(err) if Instant::now() >= deadline => return Err(err),
            Err(_) => sleep(Duration::from_millis(200)).await,
        }
    }
}

#[cfg(target_os = "linux")]
fn probe_executor(guest_cid: u32) -> Result<(), String> {
    let _ = ExecutorConn::connect_vsock(guest_cid)?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub async fn wait_executor(_guest_cid: u32, _timeout: Duration) -> Result<(), String> {
    Err(format!(
        "vsock is only supported on Linux (executor port {})",
        super::config::EXECUTOR_VSOCK_PORT
    ))
}
