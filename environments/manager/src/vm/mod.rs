//! Firecracker VM boot and teardown for one rollout environment.

mod config;
mod executor;
mod firecracker;
mod jailer;
mod paths;
mod vsock;

pub use executor::ExecutorConn;
pub use paths::{
    cache_root, join_and_verify, resolve_kernel, run_root, scratch_path,
    scratch_template_path, VmPaths,
};

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

use tokio::process::Child;

use crate::catalog::TaskSpec;

static NEXT_GUEST_CID: AtomicU32 = AtomicU32::new(3);

/// Whether this manager process should boot real VMs (Linux + `GRL_VM_BOOT` unset/1).
pub fn boot_enabled() -> bool {
    if !cfg!(target_os = "linux") {
        return false;
    }
    !matches!(
        std::env::var("GRL_VM_BOOT").as_deref(),
        Ok("0") | Ok("false") | Ok("no")
    )
}

#[derive(Debug)]
pub struct VmHandle {
    pub guest_cid: u32,
    pub run_dir: PathBuf,
    pub executor: Arc<ExecutorConn>,
    child: Child,
}

impl VmHandle {
    pub async fn stop(mut self) {
        let _ = self.child.start_kill();
        let _ = self.child.wait().await;
        let _ = std::fs::remove_dir_all(&self.run_dir);
    }

    #[cfg(test)]
    pub fn for_test(executor: Arc<ExecutorConn>, child: Child) -> Self {
        Self {
            guest_cid: 3,
            run_dir: PathBuf::from("/tmp/grl-test-vm"),
            executor,
            child,
        }
    }
}

fn alloc_guest_cid() -> u32 {
    NEXT_GUEST_CID.fetch_add(1, Ordering::Relaxed)
}

pub async fn boot(env_id: &str, spec: &TaskSpec) -> Result<VmHandle, String> {
    let cache = cache_root();
    let paths = spec.resolve_vm_paths(&cache)?;

    let guest_cid = alloc_guest_cid();
    let run_dir = run_root().join(env_id);
    std::fs::create_dir_all(&run_dir)
        .map_err(|e| format!("mkdir {}: {e}", run_dir.display()))?;
    let api_sock = run_dir.join("firecracker.sock");
    let vsock_uds = run_dir.join("vsock.sock");
    let _ = std::fs::remove_file(&api_sock);
    let _ = std::fs::remove_file(&vsock_uds);

    // Per-VM writable scratch: copy the node-local template into the run dir.
    // std::fs::copy uses copy_file_range on Linux, giving a reflink/CoW copy on
    // filesystems that support it and a fast sparse copy otherwise — the
    // journal-less template's real footprint is a few MB.
    let template = scratch_template_path(&cache);
    let scratch = scratch_path(&run_dir);
    std::fs::copy(&template, &scratch).map_err(|e| {
        format!(
            "copy scratch template {} -> {}: {e}",
            template.display(),
            scratch.display()
        )
    })?;

    let child = jailer::spawn(env_id, &api_sock).await?;
    firecracker::wait_for_socket(&api_sock, Duration::from_secs(10)).await?;

    firecracker::put(&api_sock, "machine-config", &config::machine_config()).await?;
    firecracker::put(&api_sock, "boot-source", &config::boot_source(&paths)).await?;
    firecracker::put(&api_sock, "drives/rootfs", &config::root_drive(&paths)).await?;
    firecracker::put(&api_sock, "drives/task", &config::task_drive(&paths)).await?;
    firecracker::put(&api_sock, "drives/scratch", &config::scratch_drive(&scratch)).await?;
    firecracker::put(
        &api_sock,
        "vsock",
        &config::vsock(guest_cid, &vsock_uds),
    )
    .await?;
    firecracker::put(&api_sock, "actions", &config::instance_start()).await?;

    let boot_timeout = Duration::from_secs(
        std::env::var("GRL_VM_BOOT_TIMEOUT_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(120),
    );
    vsock::wait_executor(guest_cid, boot_timeout).await?;
    let executor = connect_executor(guest_cid)?;

    Ok(VmHandle {
        guest_cid,
        run_dir,
        executor,
        child,
    })
}

#[cfg(target_os = "linux")]
fn connect_executor(guest_cid: u32) -> Result<Arc<ExecutorConn>, String> {
    Ok(Arc::new(ExecutorConn::connect_vsock(guest_cid)?))
}

#[cfg(not(target_os = "linux"))]
fn connect_executor(_guest_cid: u32) -> Result<Arc<ExecutorConn>, String> {
    Err("vsock executor connection requires Linux".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boot_disabled_off_linux() {
        if !cfg!(target_os = "linux") {
            assert!(!boot_enabled());
        }
    }
}
