//! Task catalog: the manager's read-only view of an environment's `tasks.jsonl`.
//!
//! The manager is environment-agnostic — it never parses SWE-bench rows. The
//! environment's build tooling (`vms`) renders each task's opening prompt and
//! tool schemas into `tasks.jsonl`; the manager loads that file and, on
//! `CreateEnvironment`, hands the matching task's `messages`/`tools` back to the
//! trainer verbatim and boots VM images from `base_image`/`task_image`. They are
//! opaque JSON to the manager.
//!
//! The file is synced onto the node by the standalone `bundle-sync` DaemonSet
//! (the launcher-owned `environments` Helm chart) into
//! ``{GRL_VM_CACHE_DIR}/{GRL_ACTIVE_DIR}/tasks.jsonl``; its path is given by
//! ``GRL_TASKS_FILE``. The manager starts even when the file is absent (empty
//! catalog) and hot-reloads it when the bundle's ``.ready`` sentinel appears —
//! see [`crate::reload`]. It never restarts to pick up a new bundle.

use std::collections::HashMap;

use crate::vm::{join_and_verify, resolve_kernel, VmPaths};

/// One catalog entry: prompt/tools for the trainer plus VM image paths for boot.
#[derive(Clone, Debug, Default)]
pub struct TaskSpec {
    /// JSON array of OpenAI-style chat messages.
    pub initial_messages_json: String,
    /// JSON array of tool/function schemas.
    pub tools_json: String,
    /// Split label from tasks.jsonl (may be empty).
    pub split: String,
    /// Base squashfs path relative to `GRL_VM_CACHE_DIR`.
    pub base_image: String,
    /// Task squashfs path relative to `GRL_VM_CACHE_DIR`.
    pub task_image: String,
}

impl TaskSpec {
    /// Resolve absolute kernel and image paths under the node cache root.
    pub fn resolve_vm_paths(&self, cache_root: &std::path::Path) -> Result<VmPaths, String> {
        let kernel = resolve_kernel(cache_root)?;
        let base_image = join_and_verify(cache_root, &self.base_image, "base_image")?;
        let task_image = join_and_verify(cache_root, &self.task_image, "task_image")?;
        Ok(VmPaths {
            kernel,
            base_image,
            task_image,
        })
    }
}

#[derive(Debug, Default)]
pub struct Catalog {
    tasks: HashMap<String, TaskSpec>,
}

impl Catalog {
    /// Load the catalog from the path in `GRL_TASKS_FILE`. An unset var yields
    /// an empty catalog so the manager still starts (lookups then 404).
    pub fn from_env() -> Result<Catalog, String> {
        match std::env::var("GRL_TASKS_FILE") {
            Ok(path) => Catalog::from_file(&path),
            Err(_) => Ok(Catalog::default()),
        }
    }

    /// Load from ``path``. A missing file yields an empty catalog (no bundle
    /// synced yet) so the manager still starts; a file that exists but is
    /// malformed is still an error so a broken sync never silently empties a
    /// good catalog on reload.
    pub fn from_file(path: &str) -> Result<Catalog, String> {
        if !std::path::Path::new(path).exists() {
            return Ok(Catalog::default());
        }
        let raw =
            std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
        Catalog::from_jsonl(&raw)
    }

    /// Parse newline-delimited task records. Blank lines are skipped.
    pub fn from_jsonl(contents: &str) -> Result<Catalog, String> {
        let mut tasks = HashMap::new();
        for (i, line) in contents.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let row: serde_json::Value = serde_json::from_str(line)
                .map_err(|e| format!("tasks.jsonl line {}: {e}", i + 1))?;
            let task_id = row
                .get("task_id")
                .and_then(|v| v.as_str())
                .ok_or_else(|| format!("tasks.jsonl line {}: missing task_id", i + 1))?
                .to_string();
            // Re-serialize the nested JSON to the opaque strings the proto carries.
            let initial_messages_json = row
                .get("messages")
                .map(|v| v.to_string())
                .unwrap_or_else(|| "[]".to_string());
            let tools_json = row
                .get("tools")
                .map(|v| v.to_string())
                .unwrap_or_else(|| "[]".to_string());
            let split = row
                .get("split")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            let base_image = row
                .get("base_image")
                .and_then(|v| v.as_str())
                .ok_or_else(|| {
                    format!("tasks.jsonl line {}: missing base_image", i + 1)
                })?
                .to_string();
            let task_image = row
                .get("task_image")
                .and_then(|v| v.as_str())
                .ok_or_else(|| {
                    format!("tasks.jsonl line {}: missing task_image", i + 1)
                })?
                .to_string();
            tasks.insert(
                task_id,
                TaskSpec {
                    initial_messages_json,
                    tools_json,
                    split,
                    base_image,
                    task_image,
                },
            );
        }
        Ok(Catalog { tasks })
    }

    pub fn get(&self, task_id: &str) -> Option<&TaskSpec> {
        self.tasks.get(task_id)
    }

    pub fn len(&self) -> usize {
        self.tasks.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tasks.is_empty()
    }

    /// Task index for ListTasks. When ``split_filter`` is set, omit other splits.
    pub fn list_tasks(&self, split_filter: Option<&str>) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = self
            .tasks
            .iter()
            .filter_map(|(task_id, spec)| {
                if let Some(want) = split_filter {
                    if !want.is_empty() && spec.split != want {
                        return None;
                    }
                }
                Some((task_id.clone(), spec.split.clone()))
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_line(task_id: &str, split: &str) -> String {
        format!(
            r#"{{"task_id":"{task_id}","split":"{split}","messages":[{{"role":"user","content":"hi"}}],"tools":[{{"type":"function"}}],"base_image":"images/bases/base.squashfs","task_image":"images/tasks/{task_id}.squashfs"}}"#
        )
    }

    #[test]
    fn parses_messages_and_tools_into_opaque_json() {
        let jsonl = format!(
            "{}\n\n{}\n",
            sample_line("a", "dev"),
            r#"{"task_id":"b","messages":[],"tools":[],"base_image":"images/bases/b.squashfs","task_image":"images/tasks/b.squashfs"}"#,
        );
        let catalog = Catalog::from_jsonl(&jsonl).unwrap();
        assert_eq!(catalog.len(), 2);
        let a = catalog.get("a").unwrap();
        assert_eq!(a.initial_messages_json, r#"[{"content":"hi","role":"user"}]"#);
        assert_eq!(a.tools_json, r#"[{"type":"function"}]"#);
        assert_eq!(a.base_image, "images/bases/base.squashfs");
        assert_eq!(a.task_image, "images/tasks/a.squashfs");
        assert!(catalog.get("missing").is_none());
    }

    #[test]
    fn missing_task_id_is_an_error() {
        assert!(Catalog::from_jsonl(r#"{"messages":[]}"#).is_err());
    }

    #[test]
    fn from_file_missing_yields_empty_catalog() {
        let path = std::env::temp_dir().join(format!(
            "grl-nonexistent-{}-tasks.jsonl",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);
        let catalog = Catalog::from_file(path.to_string_lossy().as_ref()).unwrap();
        assert!(catalog.is_empty());
    }

    #[test]
    fn from_file_existing_but_malformed_is_an_error() {
        let path = std::env::temp_dir().join(format!(
            "grl-malformed-{}-tasks.jsonl",
            std::process::id()
        ));
        std::fs::write(&path, "{ not json").unwrap();
        assert!(Catalog::from_file(path.to_string_lossy().as_ref()).is_err());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn missing_image_fields_are_an_error() {
        assert!(Catalog::from_jsonl(r#"{"task_id":"x","messages":[],"tools":[]}"#).is_err());
    }

    #[test]
    fn resolve_vm_paths_joins_cache_root() {
        let dir = std::env::temp_dir().join(format!("grl-catalog-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("images/bases")).unwrap();
        std::fs::create_dir_all(dir.join("images/tasks")).unwrap();
        std::fs::create_dir_all(dir.join("kernel")).unwrap();
        std::fs::write(dir.join("kernel/vmlinux-test"), b"k").unwrap();
        std::fs::write(dir.join("images/bases/base.squashfs"), b"b").unwrap();
        std::fs::write(dir.join("images/tasks/t1.squashfs"), b"t").unwrap();

        let jsonl = format!("{}\n", sample_line("t1", "dev"));
        let catalog = Catalog::from_jsonl(&jsonl).unwrap();
        let spec = catalog.get("t1").unwrap();
        let paths = spec.resolve_vm_paths(&dir).unwrap();
        assert!(paths.kernel.ends_with("vmlinux-test"));
        assert!(paths.base_image.ends_with("images/bases/base.squashfs"));
        assert!(paths.task_image.ends_with("images/tasks/t1.squashfs"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn list_tasks_filters_by_split() {
        let jsonl = concat!(
            r#"{"task_id":"a","split":"dev","messages":[],"tools":[],"base_image":"images/bases/a.squashfs","task_image":"images/tasks/a.squashfs"}"#,
            "\n",
            r#"{"task_id":"b","split":"test","messages":[],"tools":[],"base_image":"images/bases/b.squashfs","task_image":"images/tasks/b.squashfs"}"#,
            "\n",
            r#"{"task_id":"c","split":"dev","messages":[],"tools":[],"base_image":"images/bases/c.squashfs","task_image":"images/tasks/c.squashfs"}"#,
            "\n",
        );
        let catalog = Catalog::from_jsonl(jsonl).unwrap();
        let all = catalog.list_tasks(None);
        assert_eq!(all.len(), 3);
        let dev = catalog.list_tasks(Some("dev"));
        assert_eq!(dev, vec![("a".into(), "dev".into()), ("c".into(), "dev".into())]);
    }
}
