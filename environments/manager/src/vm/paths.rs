//! Resolve kernel and image paths under the node-local VM cache.

use std::path::{Path, PathBuf};

/// Absolute paths passed to Firecracker for one boot. `base_image`/`task_image`
/// are read-only squashfs lowers; the per-VM writable scratch lives in the run
/// dir and is resolved separately at boot (see [`scratch_path`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VmPaths {
    pub kernel: PathBuf,
    pub initrd: PathBuf,
    pub base_image: PathBuf,
    pub task_image: PathBuf,
    pub environment_image: PathBuf,
}

/// Node-local cache root (`GRL_VM_CACHE_DIR`, default `/var/lib/grl`).
pub fn cache_root() -> PathBuf {
    std::env::var("GRL_VM_CACHE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/var/lib/grl"))
}

/// Per-VM runtime state (API socket, config) lives here.
pub fn run_root() -> PathBuf {
    std::env::var("GRL_VM_RUN_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/var/run/grl/vms"))
}

/// Per-VM writable scratch disk under the VM's run dir. Copied from the
/// node-local template before boot and discarded with the run dir on teardown.
pub fn scratch_path(run_dir: &Path) -> PathBuf {
    run_dir.join("scratch.ext4")
}

/// Node-local pre-formatted, journal-less ext4 scratch template. Staged once
/// per node by the vm-image-cache initContainer.
pub fn scratch_template_path(cache_root: &Path) -> PathBuf {
    cache_root.join("scratch-template.ext4")
}

pub fn resolve_kernel(cache_root: &Path) -> Result<PathBuf, String> {
    if let Ok(p) = std::env::var("GRL_KERNEL_FILE") {
        let path = PathBuf::from(&p);
        if path.is_file() {
            return Ok(path);
        }
        return Err(format!("GRL_KERNEL_FILE not found: {}", path.display()));
    }
    let kernel_dir = cache_root.join("kernel");
    let mut matches: Vec<PathBuf> = std::fs::read_dir(&kernel_dir)
        .map_err(|e| format!("read {}: {e}", kernel_dir.display()))?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("vmlinux"))
        })
        .collect();
    matches.sort();
    matches.into_iter().next().ok_or_else(|| {
        format!(
            "no vmlinux* kernel under {} (set GRL_KERNEL_FILE to override)",
            kernel_dir.display()
        )
    })
}

/// Resolve the required external bootstrap initramfs.
pub fn resolve_initrd(cache_root: &Path) -> Result<PathBuf, String> {
    if let Ok(value) = std::env::var("GRL_INITRD_FILE") {
        let path = PathBuf::from(value);
        return path
            .is_file()
            .then_some(path.clone())
            .ok_or_else(|| format!("GRL_INITRD_FILE not found: {}", path.display()));
    }
    let path = cache_root.join("bootstrap/active.cpio.gz");
    path.is_file()
        .then_some(path.clone())
        .ok_or_else(|| format!("required bootstrap not found: {}", path.display()))
}

pub fn join_and_verify(cache_root: &Path, relative: &str, label: &str) -> Result<PathBuf, String> {
    let path = cache_root.join(relative);
    if path.is_file() {
        Ok(path)
    } else {
        Err(format!("{label} not found: {}", path.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn resolve_kernel_prefers_env_override() {
        let dir = std::env::temp_dir().join(format!("grl-paths-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let kernel = dir.join("vmlinux-custom");
        fs::write(&kernel, b"k").unwrap();
        unsafe {
            std::env::set_var("GRL_KERNEL_FILE", kernel.to_str().unwrap());
        }
        let resolved = resolve_kernel(&dir).unwrap();
        assert_eq!(resolved, kernel);
        unsafe {
            std::env::remove_var("GRL_KERNEL_FILE");
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn scratch_paths_are_derived_from_dirs() {
        let run_dir = PathBuf::from("/var/run/grl/vms/env-7");
        assert_eq!(
            scratch_path(&run_dir),
            PathBuf::from("/var/run/grl/vms/env-7/scratch.ext4")
        );
        let cache = PathBuf::from("/var/lib/grl");
        assert_eq!(
            scratch_template_path(&cache),
            PathBuf::from("/var/lib/grl/scratch-template.ext4")
        );
    }

    #[test]
    fn resolve_initrd_is_required() {
        let dir = std::env::temp_dir().join(format!("grl-paths-initrd-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("bootstrap")).unwrap();
        assert!(resolve_initrd(&dir).is_err());
        let initrd = dir.join("bootstrap/active.cpio.gz");
        fs::write(&initrd, b"initrd").unwrap();
        assert_eq!(resolve_initrd(&dir).unwrap(), initrd);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn resolve_kernel_globs_vmlinux_under_cache() {
        let dir = std::env::temp_dir().join(format!("grl-paths-kernel-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let kernel_dir = dir.join("kernel");
        fs::create_dir_all(&kernel_dir).unwrap();
        fs::write(kernel_dir.join("vmlinux-5.10"), b"k").unwrap();
        let resolved = resolve_kernel(&dir).unwrap();
        assert!(resolved.ends_with("vmlinux-5.10"));
        let _ = fs::remove_dir_all(&dir);
    }
}
