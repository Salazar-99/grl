//! Reward computation for swebench-lite, run inside the VM.
//!
//! The environment — not the trainer — owns the reward, because scoring a
//! SWE-bench instance means running its held-out test suite against whatever
//! the policy left in the working tree. The answer key (which tests must pass,
//! the test patch, the test command) is baked into the task image at
//! [`TASK_SPEC_PATH`] by `vms build-tasks`, so it never travels to the trainer.
//!
//! Scoring applies the test patch, runs the targeted tests once through the
//! environment's persistent shell, and parses pytest's `-rA` summary. A task is
//! "resolved" (reward 1.0) iff every FAIL_TO_PASS and PASS_TO_PASS test passes;
//! anything else is 0.0 — the SWE-bench resolution criterion.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

use crate::pb::ScoreResponse;
use crate::session::Sessions;

/// Where `vms build-tasks` writes the per-task answer key inside the VM.
const TASK_SPEC_PATH: &str = "/grl/task.json";

/// Scoring runs a whole (sub)suite, which can be slow; give it room.
const SCORE_TIMEOUT: Duration = Duration::from_secs(900);

/// Heredoc delimiter for piping the test patch into the shell without quoting it.
const PATCH_EOF: &str = "__GRL_PATCH_EOF__";

#[derive(Debug, Deserialize)]
pub struct TaskSpec {
    #[serde(default)]
    pub test_cmd: String,
    #[serde(default)]
    pub test_patch: String,
    #[serde(default)]
    pub fail_to_pass: Vec<String>,
    #[serde(default)]
    pub pass_to_pass: Vec<String>,
    #[serde(default = "default_repo_dir")]
    pub repo_dir: String,
}

fn default_repo_dir() -> String {
    "/testbed".to_string()
}

/// Score the environment `env_id` by running its baked-in test suite.
pub fn score(sessions: &Sessions, env_id: &str) -> ScoreResponse {
    let spec = match load_spec() {
        Ok(spec) => spec,
        Err(e) => return error_score(format!("load task spec: {e}")),
    };
    let command = build_score_command(&spec);
    match sessions.execute(env_id, &command, SCORE_TIMEOUT) {
        Ok(out) => resolve_reward(&out.content, &spec.fail_to_pass, &spec.pass_to_pass),
        Err(e) => error_score(format!("run tests: {e}")),
    }
}

fn load_spec() -> std::io::Result<TaskSpec> {
    let raw = std::fs::read_to_string(TASK_SPEC_PATH)?;
    serde_json::from_str(&raw).map_err(std::io::Error::other)
}

/// Distinct test files referenced by the target node ids (the part before `::`).
/// We run whole files rather than passing node ids as args: some SWE-bench node
/// ids contain spaces, which would break argument splitting.
fn target_files(spec: &TaskSpec) -> Vec<String> {
    let mut files = BTreeSet::new();
    for nodeid in spec.fail_to_pass.iter().chain(spec.pass_to_pass.iter()) {
        let file = nodeid.split("::").next().unwrap_or(nodeid).trim();
        if !file.is_empty() {
            files.insert(file.to_string());
        }
    }
    files.into_iter().collect()
}

/// Build the one shell command that applies the test patch and runs the suite.
fn build_score_command(spec: &TaskSpec) -> String {
    let mut cmd = format!("cd {} 2>/dev/null\n", shell_quote(&spec.repo_dir));

    if !spec.test_patch.is_empty() {
        // Quoted heredoc: the patch is passed through verbatim, no expansion.
        // `git apply` works without a .git dir; fall back to `patch` if absent.
        cmd.push_str(&format!(
            "cat > /tmp/grl_test.patch <<'{PATCH_EOF}'\n{}\n{PATCH_EOF}\n",
            spec.test_patch
        ));
        cmd.push_str(
            "git apply -p1 /tmp/grl_test.patch 2>/dev/null || patch -p1 < /tmp/grl_test.patch 2>/dev/null || true\n",
        );
    }

    let files = target_files(spec)
        .iter()
        .map(|f| shell_quote(f))
        .collect::<Vec<_>>()
        .join(" ");
    let test_cmd = if spec.test_cmd.is_empty() {
        "pytest -rA"
    } else {
        &spec.test_cmd
    };
    // Don't let a non-zero pytest exit (failing tests) abort the call; we read
    // the outcome from the summary, not the exit code.
    cmd.push_str(&format!("{test_cmd} {files} 2>&1 || true\n"));
    cmd
}

/// Parse a pytest `-rA` summary into reward + a JSON breakdown.
///
/// `-rA` prints one summary line per test as `OUTCOME <nodeid>` (e.g.
/// `PASSED tests/test_x.py::test_y`). Resolution requires every targeted test
/// to be PASSED; a target we never see in the output counts as not passed.
pub fn resolve_reward(output: &str, fail_to_pass: &[String], pass_to_pass: &[String]) -> ScoreResponse {
    let outcomes = parse_outcomes(output);

    let mut missing: Vec<&str> = Vec::new();
    let mut failed: Vec<&str> = Vec::new();
    let mut passed = 0usize;

    for nodeid in fail_to_pass.iter().chain(pass_to_pass.iter()) {
        match outcomes.get(nodeid.as_str()) {
            Some(o) if o == "PASSED" => passed += 1,
            Some(_) => failed.push(nodeid),
            None => missing.push(nodeid),
        }
    }

    let total = fail_to_pass.len() + pass_to_pass.len();
    let resolved = total > 0 && failed.is_empty() && missing.is_empty();
    let reward = if resolved { 1.0 } else { 0.0 };

    let detail = json!({
        "resolved": resolved,
        "total": total,
        "passed": passed,
        "failed": failed,
        "missing": missing,
    });
    ScoreResponse {
        reward,
        detail_json: detail.to_string(),
    }
}

/// Map node id -> outcome from `-rA` summary lines. Later lines win, matching
/// pytest's own summary (the last reported outcome is authoritative).
fn parse_outcomes(output: &str) -> BTreeMap<String, String> {
    const OUTCOMES: [&str; 5] = ["PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL"];
    let mut map = BTreeMap::new();
    for line in output.lines() {
        let line = line.trim();
        for outcome in OUTCOMES {
            if let Some(rest) = line.strip_prefix(outcome) {
                if let Some(nodeid) = rest.strip_prefix(' ') {
                    let nodeid = nodeid.trim();
                    if !nodeid.is_empty() {
                        map.insert(nodeid.to_string(), outcome.to_string());
                    }
                }
                break;
            }
        }
    }
    map
}

pub fn error_score(message: String) -> ScoreResponse {
    ScoreResponse {
        reward: 0.0,
        detail_json: json!({ "error": message }).to_string(),
    }
}

/// Single-quote a string for safe use as one shell word.
fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn all_targets_passing_resolves() {
        let output = "\
PASSED tests/test_a.py::test_one
PASSED tests/test_a.py::test_two
PASSED tests/test_b.py::test_three";
        let resp = resolve_reward(
            output,
            &ids(&["tests/test_a.py::test_one"]),
            &ids(&["tests/test_a.py::test_two", "tests/test_b.py::test_three"]),
        );
        assert_eq!(resp.reward, 1.0);
        assert!(resp.detail_json.contains("\"resolved\":true"));
    }

    #[test]
    fn a_failing_fail_to_pass_gives_zero() {
        let output = "\
FAILED tests/test_a.py::test_one
PASSED tests/test_a.py::test_two";
        let resp = resolve_reward(
            output,
            &ids(&["tests/test_a.py::test_one"]),
            &ids(&["tests/test_a.py::test_two"]),
        );
        assert_eq!(resp.reward, 0.0);
    }

    #[test]
    fn a_regressed_pass_to_pass_gives_zero() {
        let output = "\
PASSED tests/test_a.py::test_one
FAILED tests/test_a.py::test_two";
        let resp = resolve_reward(
            output,
            &ids(&["tests/test_a.py::test_one"]),
            &ids(&["tests/test_a.py::test_two"]),
        );
        assert_eq!(resp.reward, 0.0);
    }

    #[test]
    fn a_target_absent_from_output_gives_zero() {
        let output = "PASSED tests/test_a.py::test_one";
        let resp = resolve_reward(
            output,
            &ids(&["tests/test_a.py::test_one"]),
            &ids(&["tests/test_a.py::test_missing"]),
        );
        assert_eq!(resp.reward, 0.0);
        assert!(resp.detail_json.contains("test_missing"));
    }

    #[test]
    fn empty_targets_never_resolve() {
        let resp = resolve_reward("", &[], &[]);
        assert_eq!(resp.reward, 0.0);
    }

    #[test]
    fn score_command_applies_patch_and_targets_files() {
        let spec = TaskSpec {
            test_cmd: "pytest -rA".into(),
            test_patch: "diff --git a/x b/x".into(),
            fail_to_pass: ids(&["dir/test_a.py::t1"]),
            pass_to_pass: ids(&["dir/test_a.py::t2", "other/test_b.py::t3"]),
            repo_dir: "/testbed".into(),
        };
        let cmd = build_score_command(&spec);
        assert!(cmd.contains("cd '/testbed'"));
        assert!(cmd.contains("git apply -p1"));
        assert!(cmd.contains("'dir/test_a.py'"));
        assert!(cmd.contains("'other/test_b.py'"));
        // Files are de-duplicated.
        assert_eq!(cmd.matches("'dir/test_a.py'").count(), 1);
    }
}
