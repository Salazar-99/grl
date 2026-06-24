"""Render the trainer-facing task dataset for swebench-lite.

The environment owns how a SWE-bench row becomes a training task, so all of the
SWE-bench-specific shaping lives here, not in the trainer:

  - ``tasks.jsonl`` — one line per instance with the opening prompt
    (``messages``) and ``tools`` the manager serves in ``CreateEnvironment``,
    plus the index fields (``task_id``, ``split``) the trainer uses to enumerate
    and shard the dataset. It carries no answer keys.
  - ``reward_spec`` — the held-out tests and how to run them. This is the
    *answer*, so it never enters ``tasks.jsonl``; it is baked into each task VM
    image (``/grl/task.json``) where only the in-VM scorer can read it.
"""

from __future__ import annotations

import json
from pathlib import Path

from vms.dockerfile import slug  # noqa: F401  (kept for parity with other modules)
from vms.versions import MAP_REPO_VERSION_TO_SPECS_PY

DEFAULT_TEST_CMD = "pytest -rA"

SYSTEM_PROMPT = (
    "You are an autonomous software engineer working inside a checked-out Git "
    "repository. Fix the issue described by the user by editing files in the "
    "repository.\n\n"
    "Use the `bash` tool to explore the codebase, make edits, and run commands. "
    "Shell state (working directory, environment variables) persists across "
    "tool calls. When you are confident the issue is resolved, call the "
    "`submit` tool to hand off for grading. Do not run the hidden grading "
    "tests yourself; they are run automatically after you submit."
)

# OpenAI/Qwen function-call schema for the in-VM bash executor
# (see environments/swebench-lite/env/src/server.rs::parse_tool).
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a bash command in the repository's persistent shell. State "
            "(cwd, env vars, activated virtualenvs) persists across calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
                "timeout_secs": {
                    "type": "integer",
                    "description": "Optional per-command timeout in seconds.",
                },
            },
            "required": ["command"],
        },
    },
}


SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": (
            "Submit your solution for grading when you believe the issue is fixed."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


def tool_schemas() -> list[dict]:
    return [BASH_TOOL, SUBMIT_TOOL]


def render_messages(row: dict) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": row["problem_statement"]},
    ]


def _test_cmd(repo: str, version: str) -> str:
    specs = MAP_REPO_VERSION_TO_SPECS_PY.get(repo, {}).get(version, {})
    return specs.get("test_cmd", DEFAULT_TEST_CMD)


def _parse_test_list(value) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS are JSON-encoded lists in the dataset."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    return [str(v) for v in json.loads(value)]


def reward_spec(row: dict) -> dict:
    """The in-VM answer key: which tests must pass and how to run them.

    Baked into the task VM image, never shipped to the trainer.
    """
    return {
        "instance_id": row["instance_id"],
        "test_cmd": _test_cmd(row["repo"], row["version"]),
        "test_patch": row.get("test_patch", ""),
        "fail_to_pass": _parse_test_list(row.get("FAIL_TO_PASS")),
        "pass_to_pass": _parse_test_list(row.get("PASS_TO_PASS")),
        # Where the manager mounts the task repo disk inside the VM. The scorer
        # cds here before applying the test patch and running the suite.
        "repo_dir": "/testbed",
    }


def task_record(row: dict, split: str) -> dict:
    """One ``tasks.jsonl`` line. Index fields for the trainer, prompt/tools for
    the manager. Deliberately excludes the reward spec."""
    return {
        "task_id": row["instance_id"],
        "split": split,
        "repo": row["repo"],
        "version": row["version"],
        "messages": render_messages(row),
        "tools": tool_schemas(),
    }


def write_tasks_jsonl(tasks: list[dict], split: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        for row in tasks:
            f.write(json.dumps(task_record(row, split)) + "\n")
    return output
