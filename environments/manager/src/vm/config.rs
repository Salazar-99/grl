//! Firecracker machine configuration for one environment boot.

use std::path::Path;

use serde_json::{json, Value};

use super::paths::VmPaths;

pub const EXECUTOR_VSOCK_PORT: u32 = 5005;

pub fn boot_args() -> String {
    std::env::var("GRL_VM_BOOT_ARGS").unwrap_or_else(|_| {
        // `ro`: the root device is a read-only squashfs (Firecracker appends
        // `root=/dev/vda` for the root drive). `init=/init` is grl-init, which
        // stacks a writable overlay and pivots into it.
        "console=ttyS0 reboot=k panic=1 pci=off ro init=/init".to_string()
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
        "path_on_host": paths.base_image.display().to_string(),
        "is_root_device": true,
        "is_read_only": true,
    })
}

pub fn task_drive(paths: &VmPaths) -> Value {
    json!({
        "drive_id": "task",
        "path_on_host": paths.task_image.display().to_string(),
        "is_root_device": false,
        "is_read_only": true,
    })
}

/// Per-VM writable ext4 scratch (overlay upper for `/`, holds `/testbed`).
/// Copied from the node-local template into the run dir before boot.
pub fn scratch_drive(scratch_path: &Path) -> Value {
    json!({
        "drive_id": "scratch",
        "path_on_host": scratch_path.display().to_string(),
        "is_root_device": false,
        "is_read_only": false,
    })
}

pub fn vsock(guest_cid: u32, uds_path: &Path) -> Value {
    json!({
        "guest_cid": guest_cid,
        "uds_path": uds_path.display().to_string(),
    })
}

pub fn instance_start() -> Value {
    json!({ "action_type": "InstanceStart" })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn sample_paths() -> VmPaths {
        VmPaths {
            kernel: PathBuf::from("/cache/kernel/vmlinux"),
            base_image: PathBuf::from("/cache/images/bases/b.squashfs"),
            task_image: PathBuf::from("/cache/images/tasks/t.squashfs"),
        }
    }

    #[test]
    fn root_drive_is_readonly_root_squashfs() {
        let d = root_drive(&sample_paths());
        assert_eq!(d["drive_id"], "rootfs");
        assert_eq!(d["is_root_device"], true);
        assert_eq!(d["is_read_only"], true);
        assert_eq!(d["path_on_host"], "/cache/images/bases/b.squashfs");
    }

    #[test]
    fn task_drive_is_readonly_nonroot() {
        let d = task_drive(&sample_paths());
        assert_eq!(d["drive_id"], "task");
        assert_eq!(d["is_root_device"], false);
        assert_eq!(d["is_read_only"], true);
    }

    #[test]
    fn scratch_drive_is_writable_nonroot() {
        let d = scratch_drive(Path::new("/run/env42/scratch.ext4"));
        assert_eq!(d["drive_id"], "scratch");
        assert_eq!(d["is_root_device"], false);
        assert_eq!(d["is_read_only"], false);
        assert_eq!(d["path_on_host"], "/run/env42/scratch.ext4");
    }

    #[test]
    fn vsock_uses_supported_firecracker_fields() {
        assert_eq!(
            vsock(42, Path::new("/run/env42/vsock.sock")),
            json!({
                "guest_cid": 42,
                "uds_path": "/run/env42/vsock.sock",
            })
        );
    }

    #[test]
    fn default_boot_args_mount_root_readonly() {
        // GRL_VM_BOOT_ARGS is unset in the default test env.
        let args = boot_args();
        assert!(args.contains(" ro "), "boot args must mark root ro: {args}");
        assert!(args.contains("init=/init"));
    }
}
