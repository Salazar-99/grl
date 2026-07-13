import subprocess
import sys
from pathlib import Path

PLATFORM = "linux/amd64"


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
) -> Path:
    """Build a read-only, zstd-compressed squashfs rootfs from the env Dockerfile.

    The Docker rootfs is exported directly into a squashfs. Boot and
    environment executables are separate artifacts and are never baked into
    repository-specific bases.
    """
    tag = f"swe-base-{name}"
    squashfs_path = output_dir / f"{name}.squashfs"
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

    container = f"swe-export-{name}"
    run(["docker", "create", "--platform", platform, "--name", container, tag])
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        # Do all filesystem assembly and packing inside a privileged container:
        # Extract the rootfs tar and pack it without boot/environment payloads.
        populate = subprocess.run(
            [
                "docker",
                "run",
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
                apt-get update && apt-get install -y squashfs-tools
                mkdir -p /rootfs
                tar -xf - -C /rootfs
                rm -f /output/{squashfs_path.name}
                mksquashfs /rootfs /output/{squashfs_path.name} -comp zstd -noappend
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

    return squashfs_path


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
        squashfs_path = output_dir / f"{name}.squashfs"
        if squashfs_path.exists() and not force:
            print(f"base image {i}/{total}: {name} (skip)")
            built.append(squashfs_path)
            continue
        print(f"base image {i}/{total}: {name}")
        built.append(
            build_base_image(name, dockerfile.parent, output_dir, platform=platform)
        )
    return built
