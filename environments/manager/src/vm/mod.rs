//! Firecracker VM boot and teardown for one rollout environment.

mod config;
mod executor;
mod firecracker;
mod jailer;
mod paths;
mod scratch;
mod snapshot;
mod vsock;

pub use executor::ExecutorConn;
pub use paths::{
    VmPaths, cache_root, join_and_verify, resolve_initrd, resolve_kernel, run_root, scratch_path,
    scratch_template_path,
};

use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::time::Duration;

use tokio::io::AsyncReadExt;
use tokio::process::Child;
use tokio::sync::watch;

use crate::catalog::TaskSpec;

static NEXT_GUEST_CID: AtomicU32 = AtomicU32::new(3);
const SERIAL_TAIL_BYTES: usize = 64 * 1024;

#[derive(Debug, Default)]
struct SerialTail {
    bytes: Mutex<Vec<u8>>,
}

impl SerialTail {
    fn append(&self, chunk: &[u8]) {
        let mut bytes = self.bytes.lock().expect("serial tail lock poisoned");
        if chunk.len() >= SERIAL_TAIL_BYTES {
            bytes.clear();
            bytes.extend_from_slice(&chunk[chunk.len() - SERIAL_TAIL_BYTES..]);
            return;
        }
        let overflow = bytes
            .len()
            .saturating_add(chunk.len())
            .saturating_sub(SERIAL_TAIL_BYTES);
        if overflow > 0 {
            bytes.drain(..overflow);
        }
        bytes.extend_from_slice(chunk);
    }

    fn sanitized(&self) -> String {
        String::from_utf8_lossy(&self.bytes.lock().expect("serial tail lock poisoned"))
            .chars()
            .map(|character| {
                if character == '\n' || character == '\r' || character == '\t' {
                    character
                } else if character.is_control() {
                    '�'
                } else {
                    character
                }
            })
            .collect::<String>()
            .trim()
            .to_string()
    }
}

fn drain_serial(child: &mut Child, tail: Arc<SerialTail>) {
    let Some(mut stdout) = child.stdout.take() else {
        return;
    };
    tokio::spawn(async move {
        let mut chunk = [0_u8; 4096];
        loop {
            match stdout.read(&mut chunk).await {
                Ok(0) | Err(_) => return,
                Ok(count) => tail.append(&chunk[..count]),
            }
        }
    });
}

fn include_serial_tail(message: String, tail: &SerialTail) -> String {
    let serial = tail.sanitized();
    if serial.is_empty() {
        message
    } else {
        format!("{message}; guest serial tail:\n{serial}")
    }
}

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
    _snapshot_lease: Option<snapshot::Lease>,
    _serial_tail: Arc<SerialTail>,
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
            _snapshot_lease: None,
            _serial_tail: Arc::new(SerialTail::default()),
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
    serial_tail: Arc<SerialTail>,
}

impl BootGuard {
    fn new(run_dir: PathBuf) -> Self {
        Self {
            child: None,
            run_dir: Some(run_dir),
            scratch_cancel: Arc::new(AtomicBool::new(false)),
            serial_tail: Arc::new(SerialTail::default()),
        }
    }

    fn scratch_cancel(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.scratch_cancel)
    }

    fn set_child(&mut self, mut child: Child) {
        drain_serial(&mut child, Arc::clone(&self.serial_tail));
        self.child = Some(child);
    }

    fn child_mut(&mut self) -> &mut Child {
        self.child.as_mut().expect("boot child must be set")
    }

    async fn restart_child(
        &mut self,
        env_id: &str,
        api_sock: &std::path::Path,
    ) -> Result<(), String> {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        let _ = std::fs::remove_file(api_sock);
        self.set_child(jailer::spawn(env_id, api_sock).await?);
        Ok(())
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

    fn into_handle(
        mut self,
        guest_cid: u32,
        executor: Arc<ExecutorConn>,
        snapshot_lease: Option<snapshot::Lease>,
    ) -> VmHandle {
        VmHandle {
            guest_cid,
            run_dir: self.run_dir.take().expect("boot run dir must be set"),
            executor,
            child: Some(self.child.take().expect("boot child must be set")),
            _snapshot_lease: snapshot_lease,
            _serial_tail: Arc::clone(&self.serial_tail),
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
    let cache_paths = spec.resolve_vm_paths(&cache)?;

    let guest_cid = alloc_guest_cid();
    let host_run_dir = run_root().join(env_id);
    if !jailer::use_jailer() {
        std::fs::create_dir_all(&host_run_dir)
            .map_err(|e| format!("mkdir {}: {e}", host_run_dir.display()))?;
    }
    let staged = jailer::stage(env_id, &host_run_dir, &cache_paths)?;
    let paths = staged.paths;
    let scratch = staged.scratch_host;
    let scratch_api = staged.scratch_api;
    let vsock_api = staged.vsock_api;
    let mut boot_guard = BootGuard::new(staged.cleanup_dir);
    let api_sock = staged.api_sock;
    let vsock_uds = staged.vsock_uds;
    let _ = std::fs::remove_file(&api_sock);
    let _ = std::fs::remove_file(&vsock_uds);

    let jailed = jailer::use_jailer();
    let mut snapshot_acquire = if snapshot::enabled() {
        match snapshot::cache_key(&cache_paths).await {
            Ok(key) => match snapshot::acquire(&cache, key).await {
                Ok(acquire) => Some(acquire),
                Err(error) => {
                    eprintln!("snapshot cache unavailable for {env_id}: {error}; cold booting");
                    None
                }
            },
            Err(error) => {
                eprintln!("snapshot key failed for {env_id}: {error}; cold booting");
                None
            }
        }
    } else {
        None
    };

    // Per-VM writable scratch: sparse/reflink copy of the node-local template.
    // Must preserve holes — std::fs::copy densifies across mounts (XFS cache →
    // overlay run dir) and a full multi-GB write per env stalls the node.
    let template = scratch_template_path(&cache);
    let (scratch_source, scratch_destination, boot_scratch) = match &snapshot_acquire {
        Some(snapshot::Acquire::Hit(entry, _)) => (
            entry.scratch(),
            scratch.clone(),
            if jailed {
                scratch_api.clone()
            } else {
                scratch.clone()
            },
        ),
        Some(snapshot::Acquire::Build(builder)) if !jailed => (
            template.clone(),
            builder.entry.scratch(),
            builder.entry.scratch(),
        ),
        Some(snapshot::Acquire::Build(_)) => {
            (template.clone(), scratch.clone(), scratch_api.clone())
        }
        None => (template, scratch.clone(), scratch_api),
    };
    {
        let src = scratch_source.clone();
        let dst = scratch_destination.clone();
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
                    scratch_source.display(),
                    scratch_destination.display()
                )
            })?;
    }

    let child = jailer::spawn(env_id, &api_sock).await?;
    boot_guard.set_child(child);
    let snapshot_copy_cancel = boot_guard.scratch_cancel();

    let sequence = async {
        let boot_timeout = boot_timeout();
        if matches!(
            snapshot_acquire.as_ref(),
            Some(snapshot::Acquire::Hit(_, _))
        ) {
            let Some(snapshot::Acquire::Hit(entry, lease)) = snapshot_acquire.take() else {
                unreachable!();
            };
            let restore = match snapshot::load(&api_sock, &entry, &boot_scratch, &vsock_api).await {
                Ok(()) => connect_executor_or_exit(&mut boot_guard, &vsock_uds, boot_timeout).await,
                Err(error) => Err(error),
            };
            match restore {
                Ok(executor) => {
                    crate::telemetry::counter("grl.manager.snapshot.restores").add(1, &[]);
                    return Ok((executor, Some(lease)));
                }
                Err(error) => {
                    crate::telemetry::counter("grl.manager.snapshot.fallbacks").add(1, &[]);
                    snapshot::invalidate(&entry);
                    snapshot::disable();
                    eprintln!(
                        "snapshot restore failed for {env_id}; invalidating and cold booting: {error}"
                    );
                    let _ = std::fs::remove_file(&vsock_uds);
                    boot_guard.restart_child(env_id, &api_sock).await?;
                    let source = scratch_template_path(&cache);
                    let destination = scratch.clone();
                    let cancel = Arc::clone(&snapshot_copy_cancel);
                    tokio::task::spawn_blocking(move || {
                        scratch::copy_scratch_template_cancelable(&source, &destination, &cancel)
                    })
                    .await
                    .map_err(|e| format!("snapshot fallback scratch copy join: {e}"))?
                    .map_err(|e| format!("snapshot fallback scratch copy: {e}"))?;
                }
            }
            drop(lease);
        }

        // The first API operation also serves as readiness: its connect phase
        // retries until Firecracker is listening. No idle probe connection is
        // opened ahead of the real request.
        firecracker::put(&api_sock, "machine-config", &config::machine_config()).await?;
        firecracker::put(&api_sock, "boot-source", &config::boot_source(&paths)).await?;
        firecracker::put(&api_sock, "drives/rootfs", &config::root_drive(&paths)).await?;
        firecracker::put(&api_sock, "drives/task", &config::task_drive(&paths)).await?;
        firecracker::put(
            &api_sock,
            "drives/environment",
            &config::environment_drive(&paths),
        )
        .await?;
        firecracker::put(
            &api_sock,
            "drives/scratch",
            &config::scratch_drive(&boot_scratch),
        )
        .await?;
        firecracker::put(&api_sock, "vsock", &config::vsock(guest_cid, &vsock_api)).await?;
        firecracker::put(&api_sock, "actions", &config::instance_start()).await?;

        if let Some(snapshot::Acquire::Build(builder)) = snapshot_acquire.take() {
            let (executor, create_result) = after_readiness(
                connect_executor_or_exit(&mut boot_guard, &vsock_uds, boot_timeout),
                || snapshot::create(&api_sock, &builder.entry),
            )
            .await?;
            drop(executor);
            if jailed {
                let source = scratch.clone();
                let destination = builder.entry.scratch();
                let cancel = Arc::clone(&snapshot_copy_cancel);
                tokio::task::spawn_blocking(move || {
                    scratch::copy_scratch_template_cancelable(&source, &destination, &cancel)
                })
                .await
                .map_err(|e| format!("persist jailed snapshot scratch join: {e}"))?
                .map_err(|e| format!("persist jailed snapshot scratch: {e}"))?;
            }
            let source = builder.entry.scratch();
            let destination = scratch.clone();
            if !jailed {
                let cancel = Arc::clone(&snapshot_copy_cancel);
                tokio::task::spawn_blocking(move || {
                    scratch::copy_scratch_template_cancelable(&source, &destination, &cancel)
                })
                .await
                .map_err(|e| format!("snapshot scratch clone join: {e}"))?
                .map_err(|e| format!("snapshot scratch clone: {e}"))?;
            }
            snapshot::activate_builder(&api_sock, &boot_scratch).await?;
            let executor =
                connect_executor_or_exit(&mut boot_guard, &vsock_uds, boot_timeout).await?;
            if let Err(error) = create_result {
                crate::telemetry::counter("grl.manager.snapshot.fallbacks").add(1, &[]);
                snapshot::disable();
                eprintln!(
                    "snapshot creation failed for {env_id}; continuing cold and disabling snapshots: {error}"
                );
                return Ok((executor, None));
            }
            let entry = builder.publish()?;
            crate::telemetry::counter("grl.manager.snapshot.builds").add(1, &[]);
            return Ok((executor, Some(snapshot::lease(&entry))));
        }

        let executor = connect_executor_or_exit(&mut boot_guard, &vsock_uds, boot_timeout).await?;
        Ok((executor, None))
    };

    let result = tokio::select! {
        result = sequence => result,
        _ = wait_cancelled(&mut cancel) => {
            Err("VM boot cancelled".into())
        },
    };

    match result {
        Ok((executor, snapshot_lease)) => {
            Ok(boot_guard.into_handle(guest_cid, Arc::new(executor), snapshot_lease))
        }
        Err(err) => {
            let serial_tail = Arc::clone(&boot_guard.serial_tail);
            boot_guard.cleanup().await;
            tokio::task::yield_now().await;
            Err(include_serial_tail(err, &serial_tail))
        }
    }
}

fn boot_timeout() -> Duration {
    Duration::from_secs(
        std::env::var("GRL_VM_BOOT_TIMEOUT_SECS")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(120),
    )
}

async fn after_readiness<R, T, Ready, Create, Created>(
    readiness: Ready,
    create: Create,
) -> Result<(R, T), String>
where
    Ready: Future<Output = Result<R, String>>,
    Create: FnOnce() -> Created,
    Created: Future<Output = T>,
{
    let ready = readiness.await?;
    let created = create().await;
    Ok((ready, created))
}

async fn connect_executor_or_exit(
    boot_guard: &mut BootGuard,
    vsock_uds: &std::path::Path,
    timeout: Duration,
) -> Result<ExecutorConn, String> {
    tokio::select! {
        result = vsock::connect_executor(vsock_uds, timeout) => result,
        status = boot_guard.child_mut().wait() => {
            match status {
                Ok(status) => Err(format!("Firecracker exited during boot with {status}")),
                Err(error) => Err(format!("wait for Firecracker during boot: {error}")),
            }
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

    #[test]
    fn serial_tail_is_bounded_and_sanitized() {
        let tail = SerialTail::default();
        tail.append(&vec![b'a'; SERIAL_TAIL_BYTES]);
        tail.append(b"bootstrap failed\x00\n");

        let serial = tail.sanitized();
        assert_eq!(
            tail.bytes.lock().expect("serial tail lock poisoned").len(),
            SERIAL_TAIL_BYTES
        );
        assert!(serial.ends_with("bootstrap failed�"));
        assert!(!serial.contains('\0'));
    }

    #[tokio::test]
    async fn snapshot_creation_runs_only_after_executor_readiness() {
        let order = Arc::new(Mutex::new(Vec::new()));
        let ready_order = Arc::clone(&order);
        let create_order = Arc::clone(&order);

        let result = after_readiness(
            async move {
                ready_order.lock().unwrap().push("ready");
                Ok::<_, String>(())
            },
            || async move {
                create_order.lock().unwrap().push("snapshot");
            },
        )
        .await;

        assert!(result.is_ok());
        assert_eq!(*order.lock().unwrap(), ["ready", "snapshot"]);
    }
}
