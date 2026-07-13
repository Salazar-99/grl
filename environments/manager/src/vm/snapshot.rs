//! Node-local golden snapshot cache for prepared task VMs.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use opentelemetry::KeyValue;
use serde_json::json;
use sha2::{Digest, Sha256};

use super::config;
use super::firecracker;
use super::jailer;
use super::paths::VmPaths;

const SNAPSHOT_FORMAT: &str = "grl-snapshot-v1";

#[derive(Clone, Eq)]
struct FileIdentity {
    path: PathBuf,
    len: u64,
    modified_nanos: u128,
}

impl PartialEq for FileIdentity {
    fn eq(&self, other: &Self) -> bool {
        self.path == other.path
            && self.len == other.len
            && self.modified_nanos == other.modified_nanos
    }
}

impl Hash for FileIdentity {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.path.hash(state);
        self.len.hash(state);
        self.modified_nanos.hash(state);
    }
}

static DIGESTS: OnceLock<Mutex<HashMap<FileIdentity, String>>> = OnceLock::new();
static SUPPORTED: AtomicBool = AtomicBool::new(true);
static ACTIVE: OnceLock<Mutex<HashMap<String, usize>>> = OnceLock::new();
static CACHE_OP: OnceLock<Mutex<()>> = OnceLock::new();

fn file_digest(path: &Path) -> Result<String, String> {
    let metadata = fs::metadata(path).map_err(|e| format!("stat {}: {e}", path.display()))?;
    let modified_nanos = metadata
        .modified()
        .unwrap_or(UNIX_EPOCH)
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let identity = FileIdentity {
        path: path.to_path_buf(),
        len: metadata.len(),
        modified_nanos,
    };
    if let Some(value) = DIGESTS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap()
        .get(&identity)
        .cloned()
    {
        return Ok(value);
    }

    let mut file = File::open(path).map_err(|e| format!("open {}: {e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|e| format!("hash {}: {e}", path.display()))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let value = format!("{:x}", hasher.finalize());
    DIGESTS
        .get()
        .unwrap()
        .lock()
        .unwrap()
        .insert(identity, value.clone());
    Ok(value)
}

pub fn enabled() -> bool {
    SUPPORTED.load(Ordering::Relaxed)
        && matches!(
            std::env::var("GRL_VM_SNAPSHOTS").as_deref(),
            Ok("1") | Ok("true") | Ok("yes")
        )
}

pub fn disable() {
    SUPPORTED.store(false, Ordering::Relaxed);
}

pub fn invalidate(entry: &Entry) {
    let _operation = CACHE_OP.get_or_init(|| Mutex::new(())).lock().unwrap();
    let _ = fs::remove_file(entry.ready());
    let active = ACTIVE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap()
        .contains_key(&entry.key);
    if active {
        let _ = fs::write(entry.dir.join(".invalid"), b"invalid\n");
        let _ = fs::write(entry.dir.with_extension("lock"), b"invalidating\n");
    } else {
        let _ = fs::remove_dir_all(&entry.dir);
    }
}

pub async fn cache_key(paths: &VmPaths) -> Result<String, String> {
    let paths = paths.clone();
    tokio::task::spawn_blocking(move || {
        let mut hasher = Sha256::new();
        hasher.update(SNAPSHOT_FORMAT);
        for path in [
            Some(paths.kernel.as_path()),
            Some(paths.initrd.as_path()),
            Some(paths.base_image.as_path()),
            Some(paths.task_image.as_path()),
            Some(paths.environment_image.as_path()),
        ]
        .into_iter()
        .flatten()
        {
            hasher.update(path.to_string_lossy().as_bytes());
            hasher.update(file_digest(path)?.as_bytes());
        }
        hasher.update(config::boot_args());
        hasher.update(config::machine_config().to_string());
        let firecracker = PathBuf::from(jailer::firecracker_bin());
        hasher.update(firecracker.to_string_lossy().as_bytes());
        if firecracker.is_file() {
            hasher.update(file_digest(&firecracker)?.as_bytes());
        }
        Ok(format!("{:x}", hasher.finalize()))
    })
    .await
    .map_err(|e| format!("snapshot key task failed: {e}"))?
}

#[derive(Clone, Debug)]
pub struct Entry {
    pub key: String,
    pub dir: PathBuf,
}

#[derive(Debug)]
pub struct Lease {
    key: String,
    dir: PathBuf,
}

impl Drop for Lease {
    fn drop(&mut self) {
        let mut active = ACTIVE
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
            .unwrap();
        if let Some(count) = active.get_mut(&self.key) {
            *count -= 1;
            if *count == 0 {
                active.remove(&self.key);
            }
        }
        let remove = !active.contains_key(&self.key) && self.dir.join(".invalid").is_file();
        drop(active);
        if remove {
            let _ = fs::remove_dir_all(&self.dir);
            let _ = fs::remove_file(self.dir.with_extension("lock"));
        }
    }
}

pub fn lease(entry: &Entry) -> Lease {
    *ACTIVE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap()
        .entry(entry.key.clone())
        .or_default() += 1;
    Lease {
        key: entry.key.clone(),
        dir: entry.dir.clone(),
    }
}

impl Entry {
    pub fn state(&self) -> PathBuf {
        self.dir.join("vm.state")
    }

    pub fn memory(&self) -> PathBuf {
        self.dir.join("memory")
    }

    pub fn scratch(&self) -> PathBuf {
        self.dir.join("prepared-scratch.ext4")
    }

    fn ready(&self) -> PathBuf {
        self.dir.join(".ready")
    }
}

pub enum Acquire {
    Hit(Entry, Lease),
    Build(BuildGuard),
}

pub struct BuildGuard {
    pub entry: Entry,
    lock: PathBuf,
    published: bool,
}

impl BuildGuard {
    pub fn publish(mut self) -> Result<Entry, String> {
        let manifest = json!({
            "format": SNAPSHOT_FORMAT,
            "key": self.entry.key,
            "created_unix": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
        });
        fs::write(
            self.entry.dir.join("manifest.json"),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .map_err(|e| format!("write snapshot manifest: {e}"))?;
        fs::write(self.entry.ready(), b"ready\n")
            .map_err(|e| format!("publish snapshot ready marker: {e}"))?;
        let _ = fs::remove_file(&self.lock);
        self.published = true;
        evict(&self.entry);
        Ok(self.entry.clone())
    }
}

impl Drop for BuildGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.lock);
        if !self.published {
            let _ = fs::remove_dir_all(&self.entry.dir);
        }
    }
}

pub async fn acquire(cache_root: &Path, key: String) -> Result<Acquire, String> {
    let root = cache_root.join("snapshots");
    fs::create_dir_all(&root).map_err(|e| format!("mkdir {}: {e}", root.display()))?;
    let entry = Entry {
        dir: root.join(&key),
        key,
    };
    let lock = entry.dir.with_extension("lock");
    let lock_result = {
        let _operation = CACHE_OP.get_or_init(|| Mutex::new(())).lock().unwrap();
        if entry.ready().is_file() {
            let _ = fs::write(entry.ready(), b"ready\n");
            crate::telemetry::counter("grl.manager.snapshot.cache")
                .add(1, &[KeyValue::new("result", "hit")]);
            let lease = lease(&entry);
            return Ok(Acquire::Hit(entry, lease));
        }
        OpenOptions::new().write(true).create_new(true).open(&lock)
    };
    match lock_result {
        Ok(mut file) => {
            crate::telemetry::counter("grl.manager.snapshot.cache")
                .add(1, &[KeyValue::new("result", "miss")]);
            let _ = writeln!(file, "{}", std::process::id());
            fs::create_dir_all(&entry.dir)
                .map_err(|e| format!("mkdir {}: {e}", entry.dir.display()))?;
            Ok(Acquire::Build(BuildGuard {
                entry,
                lock,
                published: false,
            }))
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            for _ in 0..2400 {
                if entry.ready().is_file() {
                    let _operation = CACHE_OP.get_or_init(|| Mutex::new(())).lock().unwrap();
                    if entry.ready().is_file() {
                        crate::telemetry::counter("grl.manager.snapshot.cache")
                            .add(1, &[KeyValue::new("result", "wait_hit")]);
                        let lease = lease(&entry);
                        return Ok(Acquire::Hit(entry, lease));
                    }
                }
                if !lock.exists() {
                    return Err(format!(
                        "snapshot {} was invalidated; cold booting",
                        entry.key
                    ));
                }
                let stale = fs::metadata(&lock)
                    .and_then(|metadata| metadata.modified())
                    .ok()
                    .and_then(|modified| modified.elapsed().ok())
                    .is_some_and(|age| age > Duration::from_secs(300));
                if stale {
                    let _operation = CACHE_OP.get_or_init(|| Mutex::new(())).lock().unwrap();
                    let leased = ACTIVE
                        .get_or_init(|| Mutex::new(HashMap::new()))
                        .lock()
                        .unwrap()
                        .contains_key(&entry.key);
                    if leased {
                        drop(_operation);
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        continue;
                    }
                    let _ = fs::remove_file(&lock);
                    let _ = fs::remove_dir_all(&entry.dir);
                    return Err(format!(
                        "removed stale snapshot build lock for {}; retry cold boot",
                        entry.key
                    ));
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            Err(format!("timed out waiting for snapshot {}", entry.key))
        }
        Err(error) => Err(format!("create snapshot lock {}: {error}", lock.display())),
    }
}

fn evict(current: &Entry) {
    let _operation = CACHE_OP.get_or_init(|| Mutex::new(())).lock().unwrap();
    let maximum = std::env::var("GRL_SNAPSHOT_CACHE_MAX_ENTRIES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(64);
    let Some(root) = current.dir.parent() else {
        return;
    };
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    let active = ACTIVE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap();
    let mut candidates: Vec<(SystemTime, PathBuf, String)> = entries
        .filter_map(Result::ok)
        .filter_map(|item| {
            let path = item.path();
            let key = item.file_name().to_string_lossy().to_string();
            if path == current.dir || !path.join(".ready").is_file() || active.contains_key(&key) {
                return None;
            }
            let modified = fs::metadata(path.join(".ready"))
                .and_then(|metadata| metadata.modified())
                .unwrap_or(UNIX_EPOCH);
            Some((modified, path, key))
        })
        .collect();
    let total_ready = candidates.len() + 1 + active.len();
    let remove_count = total_ready.saturating_sub(maximum);
    candidates.sort_by_key(|(modified, _, _)| *modified);
    drop(active);
    for (_, path, _) in candidates.into_iter().take(remove_count) {
        let _ = fs::remove_dir_all(path);
        crate::telemetry::counter("grl.manager.snapshot.evictions").add(1, &[]);
    }
}

fn copy_or_link(source: &Path, destination: &Path) -> Result<(), String> {
    let _ = fs::remove_file(destination);
    if fs::hard_link(source, destination).is_err() {
        fs::copy(source, destination).map_err(|error| {
            format!(
                "copy snapshot artifact {} -> {}: {error}",
                source.display(),
                destination.display()
            )
        })?;
    }
    Ok(())
}

fn runtime_file(api_sock: &Path, name: &str, direct: &Path) -> (PathBuf, PathBuf) {
    if jailer::use_jailer() {
        (
            api_sock
                .parent()
                .expect("jailed API socket has a parent")
                .join(name),
            PathBuf::from("/").join(name),
        )
    } else {
        (direct.to_path_buf(), direct.to_path_buf())
    }
}

pub async fn create(api_sock: &Path, entry: &Entry) -> Result<(), String> {
    let (state_host, state_api) = runtime_file(api_sock, "snapshot.state", &entry.state());
    let (memory_host, memory_api) = runtime_file(api_sock, "snapshot.memory", &entry.memory());
    firecracker::patch(api_sock, "vm", &json!({"state": "Paused"})).await?;
    firecracker::put_timeout(
        api_sock,
        "snapshot/create",
        &create_config(&state_api, &memory_api),
        Duration::from_secs(120),
    )
    .await?;
    if jailer::use_jailer() {
        copy_or_link(&state_host, &entry.state())?;
        copy_or_link(&memory_host, &entry.memory())?;
    }
    Ok(())
}

fn create_config(state: &Path, memory: &Path) -> serde_json::Value {
    json!({
        "snapshot_type": "Full",
        "snapshot_path": state.display().to_string(),
        "mem_file_path": memory.display().to_string(),
    })
}

pub async fn activate_builder(api_sock: &Path, scratch: &Path) -> Result<(), String> {
    firecracker::patch(
        api_sock,
        "drives/scratch",
        &json!({
            "drive_id": "scratch",
            "path_on_host": scratch.display().to_string(),
        }),
    )
    .await?;
    firecracker::patch(api_sock, "vm", &json!({"state": "Resumed"})).await
}

pub async fn load(
    api_sock: &Path,
    entry: &Entry,
    scratch: &Path,
    vsock_uds: &Path,
) -> Result<(), String> {
    let (state_host, state_api) = runtime_file(api_sock, "snapshot.state", &entry.state());
    let (memory_host, memory_api) = runtime_file(api_sock, "snapshot.memory", &entry.memory());
    if jailer::use_jailer() {
        copy_or_link(&entry.state(), &state_host)?;
        copy_or_link(&entry.memory(), &memory_host)?;
    }
    firecracker::put(
        api_sock,
        "snapshot/load",
        &load_config(&state_api, &memory_api, vsock_uds),
    )
    .await?;
    firecracker::patch(
        api_sock,
        "drives/scratch",
        &json!({
            "drive_id": "scratch",
            "path_on_host": scratch.display().to_string(),
        }),
    )
    .await?;
    firecracker::patch(api_sock, "vm", &json!({"state": "Resumed"})).await
}

fn load_config(state: &Path, memory: &Path, vsock_uds: &Path) -> serde_json::Value {
    json!({
        "snapshot_path": state.display().to_string(),
        "mem_backend": {
            "backend_type": "File",
            "backend_path": memory.display().to_string(),
        },
        "resume_vm": false,
        "vsock_override": {
            "uds_path": vsock_uds.display().to_string(),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cache_key_changes_with_artifact_contents() {
        let root = std::env::temp_dir().join(format!("grl-snapshot-key-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        for name in ["kernel", "initrd", "base", "task", "environment"] {
            fs::write(root.join(name), name).unwrap();
        }
        let paths = VmPaths {
            kernel: root.join("kernel"),
            initrd: root.join("initrd"),
            base_image: root.join("base"),
            task_image: root.join("task"),
            environment_image: root.join("environment"),
        };
        let before = cache_key(&paths).await.unwrap();
        fs::write(&paths.task_image, b"changed").unwrap();
        let after = cache_key(&paths).await.unwrap();
        assert_ne!(before, after);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn restore_config_uses_private_file_backend_and_vsock_override() {
        let entry = Entry {
            key: "key".into(),
            dir: PathBuf::from("/cache/snapshots/key"),
        };
        let config = load_config(
            &entry.state(),
            &entry.memory(),
            Path::new("/run/env/vsock.sock"),
        );
        assert_eq!(config["mem_backend"]["backend_type"], "File");
        assert_eq!(config["resume_vm"], false);
        assert_eq!(config["vsock_override"]["uds_path"], "/run/env/vsock.sock");
    }

    #[tokio::test]
    async fn publish_turns_a_locked_build_into_a_cache_hit() {
        let root =
            std::env::temp_dir().join(format!("grl-snapshot-publish-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let first = acquire(&root, "cache-key".into()).await.unwrap();
        let Acquire::Build(builder) = first else {
            panic!("first acquire must build");
        };
        fs::write(builder.entry.state(), b"state").unwrap();
        fs::write(builder.entry.memory(), b"memory").unwrap();
        fs::write(builder.entry.scratch(), b"scratch").unwrap();
        builder.publish().unwrap();

        let second = acquire(&root, "cache-key".into()).await.unwrap();
        assert!(matches!(second, Acquire::Hit(_, _)));
        drop(second);
        let _ = fs::remove_dir_all(root);
    }
}
