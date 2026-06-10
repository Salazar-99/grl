from pathlib import Path

from vms.dockerfile import slug
from vms.versions import MAP_REPO_VERSION_TO_SPECS_PY


def task_entry(
    row: dict,
    *,
    base_images_dir: str = "base-images",
    task_images_dir: str = "task-images",
) -> dict:
    base_name = slug(row["repo"], row["version"])
    specs = MAP_REPO_VERSION_TO_SPECS_PY[row["repo"]][row["version"]]
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "version": row["version"],
        "base_commit": row["base_commit"],
        "python": specs["python"],
        "base_image": f"{base_images_dir}/{base_name}.ext4",
        "task_image": f"{task_images_dir}/{row['instance_id']}.ext4",
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
