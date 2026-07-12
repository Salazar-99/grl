//! Spawn Firecracker directly or via the jailer wrapper.

use std::path::Path;
use std::process::Stdio;

use tokio::process::Command;

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

/// Start Firecracker with its API socket at `api_sock` (host path).
pub async fn spawn(env_id: &str, api_sock: &Path) -> Result<tokio::process::Child, String> {
    let api_sock = api_sock.to_string_lossy().into_owned();
    let mut command = if use_jailer() {
        let id = env_id.replace('/', "_");
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
            &api_sock,
        ]);
        command
    } else {
        let mut command = Command::new(firecracker_bin());
        command.args(["--api-sock", &api_sock]);
        command
    };

    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        // Never pipe without a reader: Firecracker would eventually block on a
        // full stderr pipe. Inherit so startup/VMM errors reach Kubernetes logs.
        .stderr(Stdio::inherit())
        // A cancelled boot task must not detach a live VMM.
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("spawn Firecracker for {env_id}: {e}"))
}
