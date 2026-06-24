//! Firecracker machine configuration for one environment boot.

use std::path::Path;

use serde_json::{json, Value};

use super::paths::VmPaths;

pub const EXECUTOR_VSOCK_PORT: u32 = 5005;

pub fn boot_args() -> String {
    std::env::var("GRL_VM_BOOT_ARGS").unwrap_or_else(|_| {
        "console=ttyS0 reboot=k panic=1 pci=off init=/init".to_string()
    })
}

pub fn machine_config() -> Value {
    let vcpu_count: u64 = std::env::var("GRL_VM_VCPUS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(2);
    let mem_size_mib: u64 = std::env::var("GRL_VM_MEM_MIB")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(2048);
    json!({
        "vcpu_count": vcpu_count,
        "mem_size_mib": mem_size_mib,
        "smt": false,
    })
}

pub fn boot_source(paths: &VmPaths) -> Value {
    json!({
        "kernel_image_path": paths.kernel.display().to_string(),
        "boot_args": boot_args(),
    })
}

pub fn root_drive(paths: &VmPaths) -> Value {
    json!({
        "drive_id": "rootfs",
        "path_on_host": paths.base_ext4.display().to_string(),
        "is_root_device": true,
        "is_read_only": false,
    })
}

pub fn task_drive(paths: &VmPaths) -> Value {
    json!({
        "drive_id": "task",
        "path_on_host": paths.task_ext4.display().to_string(),
        "is_root_device": false,
        "is_read_only": false,
    })
}

pub fn vsock(guest_cid: u32, uds_path: &Path) -> Value {
    json!({
        "guest_cid": guest_cid,
        "uds_path": uds_path.display().to_string(),
        "vsock_mode": "Unix",
    })
}

pub fn instance_start() -> Value {
    json!({ "action_type": "InstanceStart" })
}
