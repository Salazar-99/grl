//! Hot-reload of the task catalog when a new bundle is synced onto the node.
//!
//! The `bundle-sync` DaemonSet writes `tasks.jsonl` into the active dir and then
//! touches a sibling `.ready` sentinel (removing it first, so a mid-sync state
//! is never observed as ready). This module watches that sentinel and swaps a
//! freshly parsed [`Catalog`] into the shared [`ArcSwap`] with no pod restart.
//!
//! A periodic poll of the sentinel's mtime is used rather than inotify: the
//! cache is a hostPath shared across the container boundary, where inotify
//! events are unreliable, so polling is the guaranteed signal.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use arc_swap::ArcSwap;

use crate::catalog::Catalog;

/// The `.ready` sentinel sitting beside a tasks file: `{dir}/.ready`.
pub fn sentinel_path(tasks_file: &Path) -> PathBuf {
    tasks_file
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(".ready")
}

fn sentinel_mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Parse `tasks_file` and, on success, swap the new catalog into `catalog`.
/// On a parse error the previous catalog is left untouched (a broken sync must
/// never clobber a good catalog). Returns the new task count on success.
pub fn reload_catalog(catalog: &ArcSwap<Catalog>, tasks_file: &Path) -> Result<usize, String> {
    let next = Catalog::from_file(tasks_file.to_string_lossy().as_ref())?;
    let count = next.len();
    catalog.store(Arc::new(next));
    Ok(count)
}

/// Spawn a background task that reloads `catalog` whenever the bundle's `.ready`
/// sentinel changes. `initial_ready` is the sentinel mtime already reflected in
/// the catalog at startup, so the first *new* sync — not the current one — is
/// what triggers a reload.
pub fn spawn_catalog_reloader(catalog: Arc<ArcSwap<Catalog>>, tasks_file: PathBuf, poll: Duration) {
    let sentinel = sentinel_path(&tasks_file);
    let mut last_ready = sentinel_mtime(&sentinel);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(poll).await;
            let current = match sentinel_mtime(&sentinel) {
                // No sentinel: no bundle yet, or a sync is in progress (it was
                // removed before re-touch). Keep serving the current catalog.
                None => continue,
                Some(mtime) => mtime,
            };
            if Some(current) == last_ready {
                continue;
            }
            // Consume this sentinel revision regardless of outcome: a persistent
            // parse failure should log once, not every poll; the next sync
            // rewrites `.ready` with a fresh mtime and retries.
            last_ready = Some(current);
            match reload_catalog(&catalog, &tasks_file) {
                Ok(count) => {
                    println!(
                        "catalog reloaded: {count} task(s) from {}",
                        tasks_file.display()
                    );
                }
                Err(err) => {
                    eprintln!("catalog reload failed ({err}); keeping previous catalog");
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_line(task_id: &str) -> String {
        format!(
            r#"{{"task_id":"{task_id}","split":"dev","messages":[],"tools":[],"base_image":"images/bases/b.squashfs","task_image":"images/tasks/{task_id}.squashfs"}}"#
        )
    }

    #[test]
    fn reload_populates_from_written_file() {
        let dir = std::env::temp_dir().join(format!("grl-reload-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let tasks = dir.join("tasks.jsonl");

        let catalog: ArcSwap<Catalog> = ArcSwap::from_pointee(Catalog::default());
        assert_eq!(catalog.load().len(), 0);

        std::fs::write(&tasks, format!("{}\n", sample_line("t1"))).unwrap();
        let count = reload_catalog(&catalog, &tasks).unwrap();
        assert_eq!(count, 1);
        assert_eq!(catalog.load().len(), 1);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupt_file_keeps_previous_catalog() {
        let dir = std::env::temp_dir().join(format!("grl-reload-bad-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let tasks = dir.join("tasks.jsonl");

        std::fs::write(&tasks, format!("{}\n", sample_line("t1"))).unwrap();
        let catalog: ArcSwap<Catalog> = ArcSwap::from_pointee(Catalog::default());
        reload_catalog(&catalog, &tasks).unwrap();
        assert_eq!(catalog.load().len(), 1);

        // A broken sync must not empty a good catalog.
        std::fs::write(&tasks, "{ not json\n").unwrap();
        assert!(reload_catalog(&catalog, &tasks).is_err());
        assert_eq!(catalog.load().len(), 1);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sentinel_is_sibling_of_tasks_file() {
        let p = sentinel_path(Path::new("/var/lib/grl/active/tasks.jsonl"));
        assert_eq!(p, Path::new("/var/lib/grl/active/.ready"));
    }
}
