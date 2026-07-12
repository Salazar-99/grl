//! In-memory registry of environments in flight on this manager pod.

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

use opentelemetry::KeyValue;
use tokio::sync::{RwLock, watch};
use tokio::task::JoinHandle;

use crate::telemetry;
use crate::vm::ExecutorConn;
use crate::vm::VmHandle;

/// Standard submit tool name (must match task catalog and trainer).
pub const SUBMIT_TOOL: &str = "submit";

/// Lifecycle phase for one rollout environment.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EnvPhase {
    Booting,
    Ready,
    Submitted,
    Evaluated,
    Failed,
}

#[derive(Debug)]
struct EnvRecord {
    _task_id: String,
    phase: EnvPhase,
    boot: Option<BootControl>,
}

#[derive(Debug)]
pub struct BootControl {
    pub cancel: watch::Sender<bool>,
    pub task: JoinHandle<()>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RegistryError {
    Exhausted,
    NotFound(String),
    NotReady { env_id: String, phase: EnvPhase },
    AlreadySubmitted { env_id: String },
    AlreadyEvaluated { env_id: String },
    ExecuteForbidden { env_id: String, phase: EnvPhase },
}

impl fmt::Display for RegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RegistryError::Exhausted => write!(f, "manager at concurrent environment capacity"),
            RegistryError::NotFound(id) => write!(f, "environment {id} not found"),
            RegistryError::NotReady { env_id, phase } => {
                write!(f, "environment {env_id} is not ready (phase {phase:?})")
            }
            RegistryError::AlreadySubmitted { env_id } => {
                write!(f, "environment {env_id} already submitted")
            }
            RegistryError::AlreadyEvaluated { env_id } => {
                write!(f, "environment {env_id} already evaluated")
            }
            RegistryError::ExecuteForbidden { env_id, phase } => {
                write!(
                    f,
                    "environment {env_id} cannot execute tools in phase {phase:?}"
                )
            }
        }
    }
}

impl std::error::Error for RegistryError {}

/// Lock-free per-phase environment tallies, maintained alongside the `envs`
/// map so the telemetry observable gauges can read them from a synchronous
/// callback (the OTel callback can't `.await` the async `RwLock`).
#[derive(Debug, Default)]
struct PhaseCounts {
    booting: AtomicUsize,
    ready: AtomicUsize,
    submitted: AtomicUsize,
    evaluated: AtomicUsize,
    failed: AtomicUsize,
}

impl PhaseCounts {
    fn slot(&self, phase: EnvPhase) -> &AtomicUsize {
        match phase {
            EnvPhase::Booting => &self.booting,
            EnvPhase::Ready => &self.ready,
            EnvPhase::Submitted => &self.submitted,
            EnvPhase::Evaluated => &self.evaluated,
            EnvPhase::Failed => &self.failed,
        }
    }

    fn inc(&self, phase: EnvPhase) {
        self.slot(phase).fetch_add(1, Ordering::Relaxed);
    }

    fn dec(&self, phase: EnvPhase) {
        self.slot(phase).fetch_sub(1, Ordering::Relaxed);
    }

    fn transition(&self, from: EnvPhase, to: EnvPhase) {
        self.dec(from);
        self.inc(to);
    }

    fn snapshot(&self) -> [(&'static str, u64); 5] {
        [
            ("booting", self.booting.load(Ordering::Relaxed) as u64),
            ("ready", self.ready.load(Ordering::Relaxed) as u64),
            ("submitted", self.submitted.load(Ordering::Relaxed) as u64),
            ("evaluated", self.evaluated.load(Ordering::Relaxed) as u64),
            ("failed", self.failed.load(Ordering::Relaxed) as u64),
        ]
    }
}

#[derive(Debug)]
pub struct Registry {
    max_concurrent: usize,
    next_suffix: AtomicU64,
    envs: RwLock<HashMap<String, EnvRecord>>,
    vms: RwLock<HashMap<String, VmHandle>>,
    // Mirrors of the map sizes/phases kept current under the existing write
    // locks, so metric callbacks stay lock-free. See `PhaseCounts`.
    active_envs: AtomicUsize,
    active_vms: AtomicUsize,
    phase_counts: PhaseCounts,
}

impl Registry {
    pub fn from_env() -> Self {
        let max_concurrent = std::env::var("GRL_MAX_CONCURRENT_ENVS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(32);
        Self::with_capacity(max_concurrent)
    }

    pub fn with_capacity(max_concurrent: usize) -> Self {
        Self {
            max_concurrent,
            next_suffix: AtomicU64::new(0),
            envs: RwLock::new(HashMap::new()),
            vms: RwLock::new(HashMap::new()),
            active_envs: AtomicUsize::new(0),
            active_vms: AtomicUsize::new(0),
            phase_counts: PhaseCounts::default(),
        }
    }

    /// Register the environment-state observable gauges against the global
    /// meter. Call once after `telemetry::init_telemetry`; a no-op when
    /// telemetry is disabled (the global meter is then the OTel no-op).
    pub fn install_metrics(self: &Arc<Self>) {
        let meter = telemetry::meter();

        let registry = Arc::clone(self);
        meter
            .u64_observable_gauge("grl.manager.envs.active")
            .with_description("Environments in flight on this manager pod")
            .with_callback(move |observer| {
                observer.observe(registry.active_envs.load(Ordering::Relaxed) as u64, &[]);
            })
            .build();

        let registry = Arc::clone(self);
        meter
            .u64_observable_gauge("grl.manager.envs.by_phase")
            .with_description("Environment count per lifecycle phase")
            .with_callback(move |observer| {
                for (phase, count) in registry.phase_counts.snapshot() {
                    observer.observe(count, &[KeyValue::new("phase", phase)]);
                }
            })
            .build();

        let registry = Arc::clone(self);
        meter
            .u64_observable_gauge("grl.manager.vms.active")
            .with_description("Attached Firecracker VMs")
            .with_callback(move |observer| {
                observer.observe(registry.active_vms.load(Ordering::Relaxed) as u64, &[]);
            })
            .build();

        let max = self.max_concurrent as u64;
        meter
            .u64_observable_gauge("grl.manager.capacity.max")
            .with_description("Concurrent environment admission cap")
            .with_callback(move |observer| observer.observe(max, &[]))
            .build();

        let registry = Arc::clone(self);
        meter
            .f64_observable_gauge("grl.manager.capacity.utilization")
            .with_description("active_envs / max_concurrent")
            .with_callback(move |observer| {
                let used = registry.active_envs.load(Ordering::Relaxed) as f64;
                let cap = registry.max_concurrent.max(1) as f64;
                observer.observe(used / cap, &[]);
            })
            .build();
    }

    pub fn new_env_id(&self, task_id: &str) -> String {
        let n = self.next_suffix.fetch_add(1, Ordering::Relaxed);
        format!("{task_id}-{n}")
    }

    pub async fn len(&self) -> usize {
        self.envs.read().await.len()
    }

    pub async fn register_booting(&self, task_id: &str) -> Result<String, RegistryError> {
        let env_id = self.new_env_id(task_id);
        self.register_booting_with_id(task_id, env_id.clone(), None)
            .await?;
        Ok(env_id)
    }

    /// Atomically register a reserved environment ID and its boot cancellation
    /// handle. Cancellation while waiting for the lock cannot leave a record:
    /// insertion happens only after the await has completed.
    pub async fn register_booting_with_id(
        &self,
        task_id: &str,
        env_id: String,
        boot: Option<BootControl>,
    ) -> Result<(), RegistryError> {
        let mut envs = self.envs.write().await;
        if self.active_envs.load(Ordering::Relaxed) >= self.max_concurrent {
            telemetry::counter("grl.manager.admission.rejected").add(1, &[]);
            return Err(RegistryError::Exhausted);
        }
        envs.insert(
            env_id,
            EnvRecord {
                _task_id: task_id.to_string(),
                phase: EnvPhase::Booting,
                boot,
            },
        );
        self.active_envs.fetch_add(1, Ordering::Relaxed);
        self.phase_counts.inc(EnvPhase::Booting);
        Ok(())
    }

    pub async fn set_ready(&self, env_id: &str) -> Result<(), RegistryError> {
        let mut envs = self.envs.write().await;
        let record = envs
            .get_mut(env_id)
            .ok_or_else(|| RegistryError::NotFound(env_id.to_string()))?;
        if record.phase == EnvPhase::Booting {
            record.phase = EnvPhase::Ready;
            record.boot = None;
            self.phase_counts
                .transition(EnvPhase::Booting, EnvPhase::Ready);
        }
        Ok(())
    }

    pub async fn mark_failed(&self, env_id: &str) -> Result<(), RegistryError> {
        let mut envs = self.envs.write().await;
        let record = envs
            .get_mut(env_id)
            .ok_or_else(|| RegistryError::NotFound(env_id.to_string()))?;
        if record.phase == EnvPhase::Booting {
            record.phase = EnvPhase::Failed;
            record.boot = None;
            self.phase_counts
                .transition(EnvPhase::Booting, EnvPhase::Failed);
        }
        Ok(())
    }

    pub async fn mark_submitted(&self, env_id: &str) -> Result<(), RegistryError> {
        let mut envs = self.envs.write().await;
        let record = envs
            .get_mut(env_id)
            .ok_or_else(|| RegistryError::NotFound(env_id.to_string()))?;
        match record.phase {
            EnvPhase::Ready => {
                record.phase = EnvPhase::Submitted;
                self.phase_counts
                    .transition(EnvPhase::Ready, EnvPhase::Submitted);
                Ok(())
            }
            EnvPhase::Submitted => Err(RegistryError::AlreadySubmitted {
                env_id: env_id.to_string(),
            }),
            EnvPhase::Booting => Err(RegistryError::NotReady {
                env_id: env_id.to_string(),
                phase: EnvPhase::Booting,
            }),
            EnvPhase::Failed | EnvPhase::Evaluated => Err(RegistryError::ExecuteForbidden {
                env_id: env_id.to_string(),
                phase: record.phase,
            }),
        }
    }

    pub async fn mark_evaluated(&self, env_id: &str) -> Result<(), RegistryError> {
        let mut envs = self.envs.write().await;
        let record = envs
            .get_mut(env_id)
            .ok_or_else(|| RegistryError::NotFound(env_id.to_string()))?;
        match record.phase {
            EnvPhase::Ready | EnvPhase::Submitted | EnvPhase::Failed => {
                let prev = record.phase;
                record.phase = EnvPhase::Evaluated;
                self.phase_counts.transition(prev, EnvPhase::Evaluated);
                Ok(())
            }
            EnvPhase::Evaluated => Err(RegistryError::AlreadyEvaluated {
                env_id: env_id.to_string(),
            }),
            EnvPhase::Booting => Err(RegistryError::NotReady {
                env_id: env_id.to_string(),
                phase: EnvPhase::Booting,
            }),
        }
    }

    /// Atomically remove an environment, cancel any boot task, and stop its VM.
    ///
    /// The env lock is taken before the VM lock, matching `attach_ready_vm`, so
    /// teardown cannot race a late boot into attaching an unowned VM.
    pub async fn teardown(&self, env_id: &str) -> (bool, bool) {
        let (mut removed, vm) = {
            let mut envs = self.envs.write().await;
            let mut vms = self.vms.write().await;
            let removed = envs.remove(env_id);
            let vm = vms.remove(env_id);
            (removed, vm)
        };

        if let Some(boot) = removed.as_mut().and_then(|record| record.boot.take()) {
            let _ = boot.cancel.send(true);
            // Cooperative cancellation lets vm::boot kill+wait the VMM and
            // finish any bounded scratch/vsock operation before teardown
            // releases capacity or reports success.
            let _ = boot.task.await;
        }
        let stopped_vm = vm.is_some();
        if let Some(vm) = vm {
            self.active_vms.fetch_sub(1, Ordering::Relaxed);
            vm.stop().await;
        }
        if let Some(record) = &removed {
            self.active_envs.fetch_sub(1, Ordering::Relaxed);
            self.phase_counts.dec(record.phase);
        }
        (removed.is_some(), stopped_vm)
    }

    pub async fn remove(&self, env_id: &str) -> bool {
        self.teardown(env_id).await.0
    }

    /// Attach a completed VM and transition Booting → Ready as one operation.
    ///
    /// Returns the VM to the caller if teardown/failure won the race.
    pub async fn attach_ready_vm(&self, env_id: &str, vm: VmHandle) -> Result<(), VmHandle> {
        let mut envs = self.envs.write().await;
        let Some(record) = envs.get_mut(env_id) else {
            return Err(vm);
        };
        if record.phase != EnvPhase::Booting {
            return Err(vm);
        }

        let mut vms = self.vms.write().await;
        if vms.insert(env_id.to_string(), vm).is_none() {
            self.active_vms.fetch_add(1, Ordering::Relaxed);
        }
        record.phase = EnvPhase::Ready;
        record.boot = None;
        self.phase_counts
            .transition(EnvPhase::Booting, EnvPhase::Ready);
        Ok(())
    }

    pub async fn take_vm(&self, env_id: &str) -> Option<VmHandle> {
        let taken = self.vms.write().await.remove(env_id);
        if taken.is_some() {
            self.active_vms.fetch_sub(1, Ordering::Relaxed);
        }
        taken
    }

    pub async fn executor(&self, env_id: &str) -> Option<Arc<ExecutorConn>> {
        self.vms
            .read()
            .await
            .get(env_id)
            .map(|vm| Arc::clone(&vm.executor))
    }

    pub async fn phase(&self, env_id: &str) -> Result<EnvPhase, RegistryError> {
        self.envs
            .read()
            .await
            .get(env_id)
            .map(|r| r.phase)
            .ok_or_else(|| RegistryError::NotFound(env_id.to_string()))
    }

    pub async fn require_execute(&self, env_id: &str) -> Result<(), RegistryError> {
        let phase = self.phase(env_id).await?;
        match phase {
            EnvPhase::Booting => Err(RegistryError::NotReady {
                env_id: env_id.to_string(),
                phase,
            }),
            EnvPhase::Ready => Ok(()),
            EnvPhase::Submitted | EnvPhase::Evaluated | EnvPhase::Failed => {
                Err(RegistryError::ExecuteForbidden {
                    env_id: env_id.to_string(),
                    phase,
                })
            }
        }
    }

    pub async fn require_evaluate(&self, env_id: &str) -> Result<(), RegistryError> {
        let phase = self.phase(env_id).await?;
        match phase {
            EnvPhase::Booting => Err(RegistryError::NotReady {
                env_id: env_id.to_string(),
                phase,
            }),
            EnvPhase::Ready | EnvPhase::Submitted | EnvPhase::Failed => Ok(()),
            EnvPhase::Evaluated => Err(RegistryError::AlreadyEvaluated {
                env_id: env_id.to_string(),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::Arc;
    use std::time::Duration;

    use tokio::process::Command;

    #[tokio::test]
    async fn admission_cap_returns_exhausted() {
        let registry = Registry::with_capacity(1);
        let a = registry.register_booting("task-a").await.unwrap();
        assert!(registry.register_booting("task-b").await.is_err());
        registry.remove(&a).await;
        assert!(registry.register_booting("task-b").await.is_ok());
    }

    #[tokio::test]
    async fn lifecycle_transitions() {
        let registry = Registry::with_capacity(4);
        let env_id = registry.register_booting("t").await.unwrap();
        assert_eq!(registry.phase(&env_id).await.unwrap(), EnvPhase::Booting);

        registry.set_ready(&env_id).await.unwrap();
        registry.mark_submitted(&env_id).await.unwrap();
        assert!(registry.mark_submitted(&env_id).await.is_err());

        registry.mark_evaluated(&env_id).await.unwrap();
        assert!(registry.mark_evaluated(&env_id).await.is_err());

        registry.remove(&env_id).await;
        assert!(registry.phase(&env_id).await.is_err());
    }

    #[tokio::test]
    async fn evaluate_allowed_from_ready_without_submit() {
        let registry = Registry::with_capacity(1);
        let env_id = registry.register_booting("t").await.unwrap();
        registry.set_ready(&env_id).await.unwrap();
        assert!(registry.require_evaluate(&env_id).await.is_ok());
    }

    #[tokio::test]
    async fn boot_failure_marks_failed_and_blocks_execute() {
        let registry = Registry::with_capacity(1);
        let env_id = registry.register_booting("t").await.unwrap();
        registry.mark_failed(&env_id).await.unwrap();
        assert_eq!(registry.phase(&env_id).await.unwrap(), EnvPhase::Failed);
        assert!(registry.require_execute(&env_id).await.is_err());
        assert!(registry.require_evaluate(&env_id).await.is_ok());
    }

    #[tokio::test]
    async fn metric_atomics_track_lifecycle() {
        let registry = Registry::with_capacity(4);
        let env_id = registry.register_booting("t").await.unwrap();
        assert_eq!(registry.active_envs.load(Ordering::Relaxed), 1);
        assert_eq!(registry.phase_counts.booting.load(Ordering::Relaxed), 1);

        registry.set_ready(&env_id).await.unwrap();
        assert_eq!(registry.phase_counts.booting.load(Ordering::Relaxed), 0);
        assert_eq!(registry.phase_counts.ready.load(Ordering::Relaxed), 1);

        registry.mark_submitted(&env_id).await.unwrap();
        assert_eq!(registry.phase_counts.ready.load(Ordering::Relaxed), 0);
        assert_eq!(registry.phase_counts.submitted.load(Ordering::Relaxed), 1);

        registry.mark_evaluated(&env_id).await.unwrap();
        assert_eq!(registry.phase_counts.submitted.load(Ordering::Relaxed), 0);
        assert_eq!(registry.phase_counts.evaluated.load(Ordering::Relaxed), 1);

        assert!(registry.remove(&env_id).await);
        assert_eq!(registry.active_envs.load(Ordering::Relaxed), 0);
        assert_eq!(registry.phase_counts.evaluated.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn teardown_waits_for_boot_task_cancellation() {
        let registry = Registry::with_capacity(1);
        let env_id = registry.new_env_id("t");
        let (cancel, mut cancelled) = watch::channel(false);
        let observed = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let observed_by_task = Arc::clone(&observed);
        let task = tokio::spawn(async move {
            let _ = cancelled.changed().await;
            observed_by_task.store(true, Ordering::Relaxed);
        });
        registry
            .register_booting_with_id("t", env_id.clone(), Some(BootControl { cancel, task }))
            .await
            .unwrap();

        let (removed, stopped_vm) = registry.teardown(&env_id).await;
        assert!(removed);
        assert!(!stopped_vm);
        assert!(observed.load(Ordering::Relaxed));
    }

    #[tokio::test]
    async fn cancelled_registration_does_not_leak_capacity() {
        let registry = Arc::new(Registry::with_capacity(1));
        let env_id = registry.new_env_id("t");
        let lock = registry.envs.write().await;
        let registry_for_task = Arc::clone(&registry);
        let registration = tokio::spawn(async move {
            registry_for_task
                .register_booting_with_id("t", env_id, None)
                .await
        });
        tokio::task::yield_now().await;
        registration.abort();
        assert!(registration.await.unwrap_err().is_cancelled());
        drop(lock);

        assert_eq!(registry.len().await, 0);
        assert!(registry.register_booting("replacement").await.is_ok());
    }

    #[tokio::test]
    async fn completed_vm_cannot_attach_after_teardown() {
        let registry = Registry::with_capacity(1);
        let env_id = registry.register_booting("t").await.unwrap();
        assert!(registry.teardown(&env_id).await.0);

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let executor = Arc::new(
            ExecutorConn::connect_tcp(&listener.local_addr().unwrap().to_string()).unwrap(),
        );
        let child = Command::new("sleep")
            .arg("3600")
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let vm = VmHandle::for_test(executor, child);
        let rejected = registry
            .attach_ready_vm(&env_id, vm)
            .await
            .expect_err("removed environment must reject a late VM");
        rejected.stop().await;

        // Let the listener and child shutdown settle before the test runtime is
        // torn down; this also catches accidental blocking cleanup.
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}
