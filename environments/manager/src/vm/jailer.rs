//! Spawn Firecracker directly or via the jailer wrapper.

use std::fs;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::process::Command;

use super::paths::VmPaths;

#[derive(Debug)]
pub struct Staging {
    pub paths: VmPaths,
    pub api_sock: PathBuf,
    pub vsock_uds: PathBuf,
    pub vsock_api: PathBuf,
    pub scratch_host: PathBuf,
    pub scratch_api: PathBuf,
    pub cleanup_dir: PathBuf,
}

pub fn firecracker_bin() -> String {
    std::env::var("GRL_FIRECRACKER_BIN").unwrap_or_else(|_| "/usr/local/bin/firecracker".into())
}

pub fn jailer_bin() -> String {
    std::env::var("GRL_JAILER_BIN").unwrap_or_else(|_| "/usr/local/bin/jailer".into())
}

pub fn use_jailer() -> bool {
    matches!(
        std::env::var("GRL_USE_JAILER").as_deref(),
        Ok("1") | Ok("true") | Ok("yes")
    )
}

pub fn jailer_base_dir() -> String {
    std::env::var("GRL_JAILER_DIR").unwrap_or_else(|_| "/srv/jailer".into())
}

fn id(env_id: &str) -> String {
    let mut sanitized: String = env_id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || character == '-' {
                character
            } else {
                '-'
            }
        })
        .collect();
    if sanitized.is_empty() {
        return "vm".into();
    }
    if sanitized.len() > 64 {
        let mut hasher = DefaultHasher::new();
        env_id.hash(&mut hasher);
        sanitized.truncate(47);
        sanitized = format!("{sanitized}-{:016x}", hasher.finish());
    }
    sanitized
}

pub fn jail_root(env_id: &str) -> PathBuf {
    PathBuf::from(jailer_base_dir())
        .join("firecracker")
        .join(id(env_id))
        .join("root")
}

fn stage_immutable(source: &Path, destination: &Path) -> Result<(), String> {
    if let Err(link_error) = fs::hard_link(source, destination) {
        fs::copy(source, destination).map_err(|copy_error| {
            format!(
                "stage immutable artifact {} -> {} (hardlink: {link_error}; copy: {copy_error})",
                source.display(),
                destination.display()
            )
        })?;
    }
    Ok(())
}

pub fn stage(env_id: &str, run_dir: &Path, paths: &VmPaths) -> Result<Staging, String> {
    if !use_jailer() {
        return Ok(Staging {
            paths: paths.clone(),
            api_sock: run_dir.join("firecracker.sock"),
            vsock_uds: run_dir.join("vsock.sock"),
            vsock_api: run_dir.join("vsock.sock"),
            scratch_host: run_dir.join("scratch.ext4"),
            scratch_api: run_dir.join("scratch.ext4"),
            cleanup_dir: run_dir.to_path_buf(),
        });
    }

    let root = jail_root(env_id);
    let cleanup_dir = root
        .parent()
        .expect("jail root always has an id parent")
        .to_path_buf();
    let _ = fs::remove_dir_all(&cleanup_dir);
    fs::create_dir_all(&root).map_err(|e| format!("create jail root {}: {e}", root.display()))?;
    let artifacts = [
        (&paths.kernel, "kernel"),
        (&paths.initrd, "initrd"),
        (&paths.base_image, "base.squashfs"),
        (&paths.task_image, "task.squashfs"),
        (&paths.environment_image, "environment.squashfs"),
    ];
    for (source, name) in artifacts {
        stage_immutable(source, &root.join(name))?;
    }
    Ok(Staging {
        paths: VmPaths {
            kernel: PathBuf::from("/kernel"),
            initrd: PathBuf::from("/initrd"),
            base_image: PathBuf::from("/base.squashfs"),
            task_image: PathBuf::from("/task.squashfs"),
            environment_image: PathBuf::from("/environment.squashfs"),
        },
        api_sock: root.join("firecracker.sock"),
        vsock_uds: root.join("vsock.sock"),
        vsock_api: PathBuf::from("/vsock.sock"),
        scratch_host: root.join("scratch.ext4"),
        scratch_api: PathBuf::from("/scratch.ext4"),
        cleanup_dir,
    })
}

/// Start Firecracker with its API socket at `api_sock` (host path).
pub async fn spawn(env_id: &str, api_sock: &Path) -> Result<tokio::process::Child, String> {
    let host_api_sock = api_sock.to_string_lossy().into_owned();
    let mut command = if use_jailer() {
        let id = id(env_id);
        let mut command = Command::new(jailer_bin());
        command.args([
            "--id",
            &id,
            "--exec-file",
            &firecracker_bin(),
            "--uid",
            "0",
            "--gid",
            "0",
            "--chroot-base-dir",
            &jailer_base_dir(),
            "--",
            "--api-sock",
            "/firecracker.sock",
        ]);
        command
    } else {
        let mut command = Command::new(firecracker_bin());
        command.args(["--api-sock", &host_api_sock]);
        command
    };

    command
        .stdin(Stdio::null())
        // ttyS0 is Firecracker stdout. The caller continuously drains it into
        // a bounded diagnostic tail so the VMM can never block on the pipe.
        .stdout(Stdio::piped())
        // Never pipe without a reader: Firecracker would eventually block on a
        // full stderr pipe. Inherit so startup/VMM errors reach Kubernetes logs.
        .stderr(Stdio::inherit())
        // A cancelled boot task must not detach a live VMM.
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("spawn Firecracker for {env_id}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jail_id_cannot_escape_configured_root() {
        assert_eq!(id("../../rollout_task"), "------rollout-task");
        let long = id(&"task_".repeat(30));
        assert_eq!(long.len(), 64);
        assert!(
            long.chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '-')
        );
    }

    #[test]
    fn direct_staging_preserves_host_paths() {
        let paths = VmPaths {
            kernel: "/cache/kernel".into(),
            initrd: "/cache/initrd".into(),
            base_image: "/cache/base".into(),
            task_image: "/cache/task".into(),
            environment_image: "/cache/environment".into(),
        };
        let staged = stage("env", Path::new("/run/env"), &paths).unwrap();
        assert_eq!(staged.paths, paths);
        assert_eq!(staged.api_sock, Path::new("/run/env/firecracker.sock"));
        assert_eq!(staged.scratch_api, Path::new("/run/env/scratch.ext4"));
    }
}
