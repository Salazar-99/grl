from pathlib import Path

from vms.dockerfile import slug
from vms.versions import MAP_REPO_VERSION_TO_SPECS_PY

# Paths relative to GRL_VM_CACHE_DIR on environment nodes (vm-image-cache layout).
NODE_BASES_DIR = "images/bases"
NODE_TASKS_DIR = "images/tasks"

# Local build tree paths (manifest.json / developer workflows).
LOCAL_BASES_DIR = "base-images"
LOCAL_TASKS_DIR = "task-images"


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
        "base_image": f"{bases_dir}/{base_name}.ext4",
        "task_image": f"{tasks_dir}/{task_id}.ext4",
    }


def task_entry(
    row: dict,
    *,
    base_images_dir: str = "base-images",
    task_images_dir: str = "task-images",
) -> dict:
    specs = MAP_REPO_VERSION_TO_SPECS_PY[row["repo"]][row["version"]]
    paths = image_paths(
        row,
        bases_dir=base_images_dir,
        tasks_dir=task_images_dir,
    )
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "version": row["version"],
        "base_commit": row["base_commit"],
        "python": specs["python"],
        **paths,
    }


def build_manifest(
    tasks: list[dict],
    *,
    base_images_dir: str = "base-images",
    task_images_dir: str = "task-images",
) -> dict:
    return {
        "tasks": {
            row["instance_id"]: task_entry(
                row,
                base_images_dir=base_images_dir,
                task_images_dir=task_images_dir,
            )
            for row in tasks
        }
    }


def write_manifest(
    tasks: list[dict],
    output: Path,
    *,
    base_images_dir: str = "base-images",
    task_images_dir: str = "task-images",
) -> Path:
    import json

    manifest = build_manifest(
        tasks,
        base_images_dir=base_images_dir,
        task_images_dir=task_images_dir,
    )
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    return output


def load_manifest(path: Path) -> dict:
    import json

    return json.loads(path.read_text())


def resolve(path: Path, task_id: str) -> dict:
    manifest = load_manifest(path)
    try:
        return manifest["tasks"][task_id]
    except KeyError as e:
        raise SystemExit(f"unknown task: {task_id}") from e


def resolve_from_tasks_jsonl(path: Path, task_id: str) -> dict:
    """Look up a task row from tasks.jsonl (includes VM image paths)."""
    import json

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("task_id") == task_id:
            return row
    raise SystemExit(f"unknown task: {task_id}")
