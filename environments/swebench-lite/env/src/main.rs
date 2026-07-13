//! In-VM executor binary for the swebench-lite environment.

use std::sync::Arc;

use env::session::Sessions;

fn prepare_workspace() -> std::io::Result<()> {
    let task_mount =
        std::env::var("GRL_TASK_MOUNT").unwrap_or_else(|_| "/run/grl/task".to_string());
    if !std::path::Path::new(&task_mount).is_dir() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("required task mount is not a directory: {task_mount}"),
        ));
    }
    std::fs::create_dir_all("/testbed")?;
    std::fs::create_dir_all("/grl")?;
    let status = std::process::Command::new("/bin/cp")
        .args(["-a", &format!("{task_mount}/."), "/testbed/"])
        .status()?;
    if !status.success() {
        return Err(std::io::Error::other(format!(
            "copy task workspace failed with {status}"
        )));
    }
    let spec = std::path::Path::new("/testbed/grl/task.json");
    if spec.is_file() {
        std::fs::rename(spec, "/grl/task.json")?;
    }
    Ok(())
}

fn main() -> std::io::Result<()> {
    prepare_workspace()?;
    let default_listen = if cfg!(target_os = "linux") {
        "vsock:5005".to_string()
    } else {
        "127.0.0.1:5005".to_string()
    };
    let listen = std::env::var("GRL_EXECUTOR_LISTEN").unwrap_or(default_listen);

    let sessions = Arc::new(Sessions::default());
    env::server::serve(&listen, sessions)
}
