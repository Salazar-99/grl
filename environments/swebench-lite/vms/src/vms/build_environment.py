"""Build the SWE-bench guest runtime as an independent squashfs artifact."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from vms.env_binary import resolve_grl_env_binary


def _package_binary(
    binary: Path,
    name: str,
    output_dir: Path,
    *,
    platform: str = "linux/amd64",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    provisional = output_dir / f".{name}-{os.getpid()}.squashfs"

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            "-v",
            f"{binary.resolve()}:/assets/grl-env:ro",
            "-v",
            f"{output_dir.resolve()}:/output",
            "ubuntu:22.04",
            "bash",
            "-c",
            f"""
            set -euo pipefail
            apt-get update -qq
            apt-get install -y -qq squashfs-tools
            root=$(mktemp -d)
            trap 'rm -rf "$root"' EXIT
            chmod 755 "$root"
            install -m 755 /assets/grl-env "$root/entrypoint"
            mksquashfs "$root" /output/{provisional.name}.tmp \
              -comp zstd -noappend -all-root -all-time 0 -mkfs-time 0 >/dev/null
            mv /output/{provisional.name}.tmp /output/{provisional.name}
            """,
        ],
        check=True,
    )
    digest = hashlib.sha256(provisional.read_bytes()).hexdigest()[:16]
    output = output_dir / f"{name}-{digest}.squashfs"
    if output.exists():
        provisional.unlink()
    else:
        provisional.replace(output)
    return output


def build_environment_image(
    output_dir: Path,
    *,
    platform: str = "linux/amd64",
    force: bool = False,
) -> Path:
    return _package_binary(
        resolve_grl_env_binary(platform=platform, force=force),
        "swebench-lite",
        output_dir,
        platform=platform,
    )


def build_minimal_environment_image(
    output_dir: Path,
    *,
    platform: str = "linux/amd64",
    force: bool = False,
) -> Path:
    environments = Path(__file__).resolve().parents[4]
    crate = environments / "minimal-env"
    target = "x86_64-unknown-linux-musl"
    binary = crate / "target" / target / "release" / "grl-minimal-env"
    if force or not binary.is_file():
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                "-v",
                f"{environments.resolve()}:/workspace/environments",
                "-w",
                "/workspace/environments/minimal-env",
                "rust:1.93-bookworm",
                "bash",
                "-c",
                (
                    "apt-get update -qq && apt-get install -y -qq musl-tools && "
                    f"rustup target add {target} && cargo build --release --target {target}"
                ),
            ],
            check=True,
        )
    return _package_binary(
        binary,
        "minimal",
        output_dir,
        platform=platform,
    )
