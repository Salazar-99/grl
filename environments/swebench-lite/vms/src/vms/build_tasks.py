import json
from pathlib import Path

from vms.build_images import PLATFORM, _shrink_ext4_bash, run
from vms.dataset import load_tasks
from vms.tasks import reward_spec

TASK_BUILD_SIZE_MB = 150
TASK_HEADROOM_MB = 64

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
    size_mb: int = TASK_BUILD_SIZE_MB,
    headroom_mb: int = TASK_HEADROOM_MB,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ext4_path = output_dir / f"{task_id}.ext4"

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
                "--privileged",
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
                apt-get update && apt-get install -y git e2fsprogs
                git clone https://github.com/{repo}.git /tmp/repo
                cd /tmp/repo
                git checkout {base_commit}
                rm -rf .git
                dd if=/dev/zero of=/workspace/{task_id}.ext4 bs=1M count={size_mb}
                mkfs.ext4 -F /workspace/{task_id}.ext4
                mkdir -p /mnt/task
                mount /workspace/{task_id}.ext4 /mnt/task
                cp -a /tmp/repo/. /mnt/task/
                mkdir -p "/mnt/task/$(dirname {REWARD_SPEC_PATH})"
                cp /workspace/{task_id}.task.json /mnt/task/{REWARD_SPEC_PATH}
                umount /mnt/task
                {_shrink_ext4_bash(f"/workspace/{task_id}.ext4", headroom_mb)}
                """,
            ]
        )
    finally:
        spec_file.unlink(missing_ok=True)
    return ext4_path


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
        ext4_path = output_dir / f"{task_id}.ext4"
        if ext4_path.exists() and not force:
            print(f"task image {i}/{total}: {task_id} (skip)")
            built.append(ext4_path)
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
