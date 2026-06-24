//! Host-side vsock dial to the guest executor.

use std::time::Duration;

#[cfg(target_os = "linux")]
pub async fn wait_executor(guest_cid: u32, timeout: Duration) -> Result<(), String> {
    use tokio::time::{sleep, Instant};

    let deadline = Instant::now() + timeout;
    loop {
        match try_connect(guest_cid) {
            Ok(()) => return Ok(()),
            Err(err) if Instant::now() >= deadline => return Err(err),
            Err(_) => sleep(Duration::from_millis(200)).await,
        }
    }
}

#[cfg(target_os = "linux")]
fn try_connect(guest_cid: u32) -> Result<(), String> {
    use std::os::fd::AsRawFd;
    use std::time::Duration;

    use vsock::VsockStream;

    const EXECUTOR_VSOCK_PORT: u32 = super::config::EXECUTOR_VSOCK_PORT;
    let mut stream = VsockStream::connect_with_cid_port(guest_cid, EXECUTOR_VSOCK_PORT)
        .map_err(|e| format!("vsock connect cid={guest_cid} port={EXECUTOR_VSOCK_PORT}: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| format!("vsock set_read_timeout: {e}"))?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| format!("vsock set_write_timeout: {e}"))?;
    let _ = stream.as_raw_fd();
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub async fn wait_executor(_guest_cid: u32, _timeout: Duration) -> Result<(), String> {
    Err(format!(
        "vsock is only supported on Linux (executor port {})",
        super::config::EXECUTOR_VSOCK_PORT
    ))
}
