import json
from pathlib import Path

from vms.dockerfile import slug

# Paths relative to GRL_VM_CACHE_DIR on environment nodes (vm-image-cache layout).
NODE_BASES_DIR = "images/bases"
NODE_TASKS_DIR = "images/tasks"


def image_paths(
    row: dict,
    *,
    bases_dir: str,
    tasks_dir: str,
) -> dict[str, str]:
    """Return base_image and task_image paths under the given directory prefixes."""
    base_name = slug(row["repo"], row["version"])
    task_id = row.get("instance_id") or row.get("task_id")
    if not task_id:
        raise ValueError("row missing instance_id or task_id")
    return {
        "base_image": f"{bases_dir}/{base_name}.squashfs",
        "task_image": f"{tasks_dir}/{task_id}.squashfs",
    }


def resolve_from_tasks_jsonl(path: Path, task_id: str) -> dict:
    """Look up a task row from tasks.jsonl (includes VM image paths)."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("task_id") == task_id:
            return row
    raise SystemExit(f"unknown task: {task_id}")
