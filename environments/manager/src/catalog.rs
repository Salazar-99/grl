//! Task catalog: the manager's read-only view of an environment's `tasks.jsonl`.
//!
//! The manager is environment-agnostic — it never parses SWE-bench rows. The
//! environment's build tooling (`vms`) renders each task's opening prompt and
//! tool schemas into `tasks.jsonl`; the manager loads that file and, on
//! `CreateEnvironment`, hands the matching task's `messages`/`tools` back to the
//! trainer verbatim. They are opaque JSON to the manager.
//!
//! The file is provided locally (an init-container or volume mounts the object
//! `GRL_TASKS_S3_URI` points at). Its path is given by `GRL_TASKS_FILE`.

use std::collections::HashMap;

/// One catalog entry: the prompt and tools the trainer needs to start a task.
#[derive(Clone, Debug, Default)]
pub struct TaskSpec {
    /// JSON array of OpenAI-style chat messages.
    pub initial_messages_json: String,
    /// JSON array of tool/function schemas.
    pub tools_json: String,
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

    pub fn from_file(path: &str) -> Result<Catalog, String> {
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
            tasks.insert(
                task_id,
                TaskSpec {
                    initial_messages_json,
                    tools_json,
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_messages_and_tools_into_opaque_json() {
        let jsonl = concat!(
            r#"{"task_id":"a","split":"dev","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function"}]}"#,
            "\n",
            "\n", // blank line tolerated
            r#"{"task_id":"b","messages":[],"tools":[]}"#,
            "\n",
        );
        let catalog = Catalog::from_jsonl(jsonl).unwrap();
        assert_eq!(catalog.len(), 2);
        let a = catalog.get("a").unwrap();
        assert_eq!(a.initial_messages_json, r#"[{"content":"hi","role":"user"}]"#);
        assert_eq!(a.tools_json, r#"[{"type":"function"}]"#);
        assert!(catalog.get("missing").is_none());
    }

    #[test]
    fn missing_task_id_is_an_error() {
        assert!(Catalog::from_jsonl(r#"{"messages":[]}"#).is_err());
    }
}
