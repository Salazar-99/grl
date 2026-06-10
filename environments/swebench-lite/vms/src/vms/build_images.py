import subprocess
import sys
from pathlib import Path

PLATFORM = "linux/amd64"
# Preallocate generously during build; filesystem is shrunk after populate.
BUILD_SIZE_MB = 2048
# Free space retained for runtime work (pip install -e ., test artifacts, etc.)
HEADROOM_MB = 512


def _shrink_ext4_bash(path: str, headroom_mb: int) -> str:
    return f"""
    e2fsck -fy {path}
    resize2fs -M {path}
    block_size=$(tune2fs -l {path} | awk '/Block size/{{print $3}}')
    block_count=$(tune2fs -l {path} | awk '/Block count/{{print $3}}')
    fs_bytes=$((block_size * block_count))
    headroom=$(( {headroom_mb} * 1024 * 1024 ))
    truncate -s $((fs_bytes + headroom)) {path}
    e2fsck -fy {path}
    resize2fs {path}
    """


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, None, result.stderr
            )
    return result


def build_base_image(
    name: str,
    dockerfile_dir: Path,
    output_dir: Path,
    *,
    platform: str = PLATFORM,
    size_mb: int = BUILD_SIZE_MB,
    headroom_mb: int = HEADROOM_MB,
) -> Path:
    tag = f"swe-base-{name}"
    ext4_path = output_dir / f"{name}.ext4"
    output_dir.mkdir(parents=True, exist_ok=True)

    run(
        [
            "docker",
            "buildx",
            "build",
            "--progress=quiet",
            "--platform",
            platform,
            "-t",
            tag,
            "--load",
            str(dockerfile_dir),
        ]
    )

    run(["dd", "if=/dev/zero", f"of={ext4_path}", "bs=1M", f"count={size_mb}"])

    container = f"swe-export-{name}"
    run(["docker", "create", "--platform", platform, "--name", container, tag])
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        populate = subprocess.run(
            [
                "docker",
                "run",
                "--privileged",
                "--rm",
                "-i",
                "--platform",
                platform,
                "-v",
                f"{output_dir.resolve()}:/output",
                "ubuntu:22.04",
                "bash",
                "-c",
                f"""
                set -euo pipefail
                apt-get update && apt-get install -y e2fsprogs
                mkfs.ext4 -F /output/{ext4_path.name}
                mkdir -p /mnt/disk
                mount /output/{ext4_path.name} /mnt/disk
                tar -xf - -C /mnt/disk
                umount /mnt/disk
                {_shrink_ext4_bash(f"/output/{ext4_path.name}", headroom_mb)}
                """,
            ],
            stdin=export.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        export.wait()
        if export.returncode != 0:
            raise subprocess.CalledProcessError(export.returncode, export.args)
        if populate.returncode != 0:
            if populate.stderr:
                print(populate.stderr, file=sys.stderr, end="")
            raise subprocess.CalledProcessError(
                populate.returncode, populate.args, None, populate.stderr
            )
    finally:
        subprocess.run(
            ["docker", "rm", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    return ext4_path


def build_all(
    dockerfiles_dir: Path,
    output_dir: Path,
    *,
    platform: str = PLATFORM,
    only: str | None = None,
    force: bool = False,
) -> list[Path]:
    dockerfiles = sorted(dockerfiles_dir.glob("*/Dockerfile"))
    if only:
        dockerfiles = [d for d in dockerfiles if d.parent.name == only]
    total = len(dockerfiles)

    built: list[Path] = []
    for i, dockerfile in enumerate(dockerfiles, start=1):
        name = dockerfile.parent.name
        ext4_path = output_dir / f"{name}.ext4"
        if ext4_path.exists() and not force:
            print(f"base image {i}/{total}: {name} (skip)")
            built.append(ext4_path)
            continue
        print(f"base image {i}/{total}: {name}")
        built.append(
            build_base_image(name, dockerfile.parent, output_dir, platform=platform)
        )
    return built
