import json
from pathlib import Path

from vms.build_images import PLATFORM, run
from vms.dataset import load_tasks
from vms.tasks import reward_spec

# Where the held-out reward spec lands inside the task disk. The in-VM scorer
# reads it from here; it never leaves the VM (see env/src/score.rs).
REWARD_SPEC_PATH = "grl/task.json"


def build_task_image(
    task_id: str,
    repo: str,
    base_commit: str,
    output_dir: Path,
    *,
    spec: dict,
    platform: str = PLATFORM,
) -> Path:
    """Pack the checked-out repo + reward spec into a read-only squashfs.

    The guest mounts this RO and copies its contents into the writable
    `/testbed` overlay at boot, so the task image itself is immutable and
    shareable across the concurrent VMs GRPO fans out per task.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    squashfs_path = output_dir / f"{task_id}.squashfs"

    # Stage the reward spec on the host and mount it in: writing arbitrary JSON
    # (with patches, quotes, newlines) through the container's shell heredoc
    # would be a quoting minefield, so we copy a file instead.
    spec_file = output_dir / f"{task_id}.task.json"
    spec_file.write_text(json.dumps(spec, indent=2))
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                "-v",
                f"{output_dir.resolve()}:/workspace",
                "ubuntu:22.04",
                "bash",
                "-c",
                f"""
                set -euo pipefail
                apt-get update && apt-get install -y git squashfs-tools
                git clone https://github.com/{repo}.git /tmp/repo
                cd /tmp/repo
                git checkout {base_commit}
                rm -rf .git
                mkdir -p "/tmp/repo/$(dirname {REWARD_SPEC_PATH})"
                cp /workspace/{task_id}.task.json /tmp/repo/{REWARD_SPEC_PATH}
                rm -f /workspace/{squashfs_path.name}
                mksquashfs /tmp/repo /workspace/{squashfs_path.name} -comp zstd -noappend
                """,
            ]
        )
    finally:
        spec_file.unlink(missing_ok=True)
    return squashfs_path


def build_all_tasks(
    dataset: Path,
    output_dir: Path,
    *,
    platform: str = PLATFORM,
    only: str | None = None,
    force: bool = False,
) -> list[Path]:
    tasks = load_tasks(dataset)
    if only:
        tasks = [t for t in tasks if t["instance_id"] == only]
    total = len(tasks)

    built: list[Path] = []
    for i, task in enumerate(tasks, start=1):
        task_id = task["instance_id"]
        squashfs_path = output_dir / f"{task_id}.squashfs"
        if squashfs_path.exists() and not force:
            print(f"task image {i}/{total}: {task_id} (skip)")
            built.append(squashfs_path)
            continue
        print(f"task image {i}/{total}: {task_id}")
        built.append(
            build_task_image(
                task_id,
                task["repo"],
                task["base_commit"],
                output_dir,
                spec=reward_spec(task),
                platform=platform,
            )
        )
    return built
