"""Build the shared base/task squashfs artifacts for string-reverse."""

from __future__ import annotations

import subprocess
from pathlib import Path

from vms.build_images import build_base_image


def build_string_reverse_images(
    output_dir: Path,
    *,
    platform: str = "linux/amd64",
    force: bool = False,
) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[4]
    base_dir = output_dir / "bases"
    task_dir = output_dir / "tasks"
    base_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    base = base_dir / "reverse.squashfs"
    task = task_dir / "reverse-common.squashfs"
    if force or not base.exists():
        base = build_base_image(
            "reverse",
            root / "string-reverse",
            base_dir,
            platform=platform,
        )
    if force or not task.exists():
        subprocess.run(
            [
                "docker", "run", "--rm", "--platform", platform,
                "-v", f"{task_dir.resolve()}:/output",
                "ubuntu:22.04", "bash", "-c",
                "set -euo pipefail; apt-get update -qq; apt-get install -y -qq squashfs-tools; "
                "root=$(mktemp -d); mksquashfs \"$root\" /output/reverse-common.squashfs "
                "-comp zstd -noappend -all-root -all-time 0 -mkfs-time 0 >/dev/null",
            ],
            check=True,
        )
    return base, task
