//! Firecracker VM boot and teardown for one rollout environment.

mod config;
mod executor;
mod firecracker;
mod jailer;
mod paths;
mod scratch;
mod vsock;

pub use executor::ExecutorConn;
pub use paths::{
    VmPaths, cache_root, join_and_verify, resolve_kernel, run_root, scratch_path,
    scratch_template_path,
};

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::time::Duration;

use tokio::process::Child;
use tokio::sync::watch;

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
    child: Option<Child>,
}

impl VmHandle {
    pub async fn stop(mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        let _ = std::fs::remove_dir_all(&self.run_dir);
    }

    #[cfg(test)]
    pub fn for_test(executor: Arc<ExecutorConn>, child: Child) -> Self {
        Self {
            guest_cid: 3,
            run_dir: PathBuf::from("/tmp/grl-test-vm"),
            executor,
            child: Some(child),
        }
    }
}

impl Drop for VmHandle {
    fn drop(&mut self) {
        if let Some(child) = self.child.take() {
            reap_in_background(child, self.run_dir.clone());
        }
    }
}

fn reap_in_background(mut child: Child, run_dir: PathBuf) {
    let _ = child.start_kill();
    if let Ok(runtime) = tokio::runtime::Handle::try_current() {
        runtime.spawn(async move {
            let _ = child.wait().await;
            let _ = std::fs::remove_dir_all(run_dir);
        });
    } else {
        // `kill_on_drop(true)` remains the fallback outside a Tokio runtime.
        drop(child);
        let _ = std::fs::remove_dir_all(run_dir);
    }
}

/// Owns a partially booted VMM until it can be promoted to `VmHandle`.
///
/// Explicit failures use `cleanup` so the child is reaped. `Drop` is the
/// cancellation/panic fallback: `start_kill` plus Command's `kill_on_drop`
/// guarantee that an aborted boot task cannot orphan Firecracker.
struct BootGuard {
    child: Option<Child>,
    run_dir: Option<PathBuf>,
    scratch_cancel: Arc<AtomicBool>,
}

impl BootGuard {
    fn new(run_dir: PathBuf) -> Self {
        Self {
            child: None,
            run_dir: Some(run_dir),
            scratch_cancel: Arc::new(AtomicBool::new(false)),
        }
    }

    fn scratch_cancel(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.scratch_cancel)
    }

    fn set_child(&mut self, child: Child) {
        self.child = Some(child);
    }

    fn child_mut(&mut self) -> &mut Child {
        self.child.as_mut().expect("boot child must be set")
    }

    async fn cleanup(mut self) {
        self.scratch_cancel.store(true, Ordering::Relaxed);
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        if let Some(run_dir) = self.run_dir.take() {
            let _ = std::fs::remove_dir_all(run_dir);
        }
    }

    fn into_handle(mut self, guest_cid: u32, executor: Arc<ExecutorConn>) -> VmHandle {
        VmHandle {
            guest_cid,
            run_dir: self.run_dir.take().expect("boot run dir must be set"),
            executor,
            child: Some(self.child.take().expect("boot child must be set")),
        }
    }
}

impl Drop for BootGuard {
    fn drop(&mut self) {
        self.scratch_cancel.store(true, Ordering::Relaxed);
        match (self.child.take(), self.run_dir.take()) {
            (Some(child), Some(run_dir)) => reap_in_background(child, run_dir),
            (_, Some(run_dir)) => {
                let _ = std::fs::remove_dir_all(run_dir);
            }
            _ => {}
        }
    }
}

fn alloc_guest_cid() -> u32 {
    NEXT_GUEST_CID.fetch_add(1, Ordering::Relaxed)
}

pub async fn boot(
    env_id: &str,
    spec: &TaskSpec,
    mut cancel: watch::Receiver<bool>,
) -> Result<VmHandle, String> {
    let cache = cache_root();
    let paths = spec.resolve_vm_paths(&cache)?;

    let guest_cid = alloc_guest_cid();
    let run_dir = run_root().join(env_id);
    std::fs::create_dir_all(&run_dir).map_err(|e| format!("mkdir {}: {e}", run_dir.display()))?;
    let mut boot_guard = BootGuard::new(run_dir.clone());
    let api_sock = run_dir.join("firecracker.sock");
    let vsock_uds = run_dir.join("vsock.sock");
    let _ = std::fs::remove_file(&api_sock);
    let _ = std::fs::remove_file(&vsock_uds);

    // Per-VM writable scratch: sparse/reflink copy of the node-local template.
    // Must preserve holes — std::fs::copy densifies across mounts (XFS cache →
    // overlay run dir) and a full multi-GB write per env stalls the node.
    let template = scratch_template_path(&cache);
    let scratch = scratch_path(&run_dir);
    {
        let src = template.clone();
        let dst = scratch.clone();
        let copy_cancel = boot_guard.scratch_cancel();
        let worker_cancel = Arc::clone(&copy_cancel);
        let mut copy = tokio::task::spawn_blocking(move || {
            scratch::copy_scratch_template_cancelable(&src, &dst, &worker_cancel)
        });
        let copy_result = tokio::select! {
            result = &mut copy => result,
            _ = wait_cancelled(&mut cancel) => {
                copy_cancel.store(true, Ordering::Relaxed);
                // The worker checks cancellation between MiB chunks. Await it
                // so teardown does not release capacity while disk I/O remains.
                let _ = copy.await;
                return Err("VM boot cancelled during scratch copy".into());
            }
        };
        copy_result
            .map_err(|e| format!("scratch copy join: {e}"))?
            .map_err(|e| {
                format!(
                    "copy scratch template {} -> {}: {e}",
                    template.display(),
                    scratch.display()
                )
            })?;
    }

    let child = jailer::spawn(env_id, &api_sock).await?;
    boot_guard.set_child(child);

    let sequence = async {
        // The first API operation also serves as readiness: its connect phase
        // retries until Firecracker is listening. No idle probe connection is
        // opened ahead of the real request.
        firecracker::put(&api_sock, "machine-config", &config::machine_config()).await?;
        firecracker::put(&api_sock, "boot-source", &config::boot_source(&paths)).await?;
        firecracker::put(&api_sock, "drives/rootfs", &config::root_drive(&paths)).await?;
        firecracker::put(&api_sock, "drives/task", &config::task_drive(&paths)).await?;
        firecracker::put(
            &api_sock,
            "drives/scratch",
            &config::scratch_drive(&scratch),
        )
        .await?;
        firecracker::put(&api_sock, "vsock", &config::vsock(guest_cid, &vsock_uds)).await?;
        firecracker::put(&api_sock, "actions", &config::instance_start()).await?;

        let boot_timeout = Duration::from_secs(
            std::env::var("GRL_VM_BOOT_TIMEOUT_SECS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(120),
        );
        vsock::connect_executor(guest_cid, boot_timeout).await
    };

    let result = tokio::select! {
        result = sequence => result,
        _ = wait_cancelled(&mut cancel) => {
            Err("VM boot cancelled".into())
        },
        status = boot_guard.child_mut().wait() => {
            match status {
                Ok(status) => Err(format!("Firecracker exited during boot with {status}")),
                Err(err) => Err(format!("wait for Firecracker during boot: {err}")),
            }
        }
    };

    match result {
        Ok(executor) => Ok(boot_guard.into_handle(guest_cid, Arc::new(executor))),
        Err(err) => {
            boot_guard.cleanup().await;
            Err(err)
        }
    }
}

async fn wait_cancelled(cancel: &mut watch::Receiver<bool>) {
    if *cancel.borrow() {
        return;
    }
    loop {
        if cancel.changed().await.is_err() || *cancel.borrow() {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_TEST_DIR: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn boot_disabled_off_linux() {
        if !cfg!(target_os = "linux") {
            assert!(!boot_enabled());
        }
    }

    #[tokio::test]
    async fn failed_boot_cleanup_reaps_child_and_removes_run_dir() {
        let run_dir = std::env::temp_dir().join(format!(
            "grl-boot-guard-test-{}-{}",
            std::process::id(),
            NEXT_TEST_DIR.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(run_dir.join("firecracker.sock"), b"test").unwrap();

        let mut command = tokio::process::Command::new("sleep");
        command.arg("3600").kill_on_drop(true);
        let child = command.spawn().unwrap();
        let mut guard = BootGuard::new(run_dir.clone());
        guard.set_child(child);
        guard.cleanup().await;

        assert!(!run_dir.exists());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn dropped_boot_guard_reaps_child_before_removing_run_dir() {
        let run_dir = std::env::temp_dir().join(format!(
            "grl-boot-drop-test-{}-{}",
            std::process::id(),
            NEXT_TEST_DIR.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&run_dir).unwrap();
        let mut command = tokio::process::Command::new("sleep");
        command.arg("3600").kill_on_drop(true);
        let child = command.spawn().unwrap();
        let pid = child.id().unwrap() as libc::pid_t;
        let mut guard = BootGuard::new(run_dir.clone());
        guard.set_child(child);
        drop(guard);

        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let process_gone = unsafe { libc::kill(pid, 0) } < 0
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
                if process_gone && !run_dir.exists() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("background reaper did not finish");
    }
}
