"""Build the final static Rust bootstrap initramfs."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

BOOTSTRAP_CRATE = Path(__file__).resolve().parents[4] / "bootstrap"


def build_bootstrap(output_dir: Path, *, platform: str = "linux/amd64") -> Path:
    if platform not in {"linux/amd64", "amd64", "x86_64"}:
        raise ValueError(f"unsupported bootstrap platform: {platform}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="grl-bootstrap-") as temp:
        temp_dir = Path(temp)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{BOOTSTRAP_CRATE.resolve()}:/src:ro",
                "-v",
                f"{temp_dir.resolve()}:/out",
                "rust:1.93-bookworm",
                "bash",
                "-c",
                """
                set -euo pipefail
                apt-get update -qq
                apt-get install -y -qq musl-tools
                rustup target add x86_64-unknown-linux-musl
                cp -a /src /tmp/bootstrap
                cd /tmp/bootstrap
                cargo build --release --target x86_64-unknown-linux-musl
                cp target/x86_64-unknown-linux-musl/release/grl-bootstrap /out/grl-bootstrap
                """,
            ],
            check=True,
        )
        binary = temp_dir / "grl-bootstrap"
        provisional = output_dir / f".grl-bootstrap-{os.getpid()}.cpio.gz"
        os.chmod(binary, 0o755)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{binary.resolve()}:/assets/init:ro",
                "-v",
                f"{output_dir.resolve()}:/output",
                "ubuntu:22.04",
                "bash",
                "-c",
                f"""
                set -euo pipefail
                apt-get update -qq
                apt-get install -y -qq cpio gzip
                root=$(mktemp -d)
                trap 'rm -rf "$root"' EXIT
                install -m 755 /assets/init "$root/init"
                (cd "$root" &&
                  find . -exec touch -h -d @0 {{}} + &&
                  find . -print0 | sort -z |
                  cpio --null -o --format=newc --owner=0:0 --reproducible 2>/dev/null |
                  gzip -n -9 > /output/{provisional.name}.tmp)
                mv /output/{provisional.name}.tmp /output/{provisional.name}
                """,
            ],
            check=True,
        )
        digest = hashlib.sha256(provisional.read_bytes()).hexdigest()[:16]
        output = output_dir / f"grl-bootstrap-{digest}.cpio.gz"
        if output.exists():
            provisional.unlink()
        else:
            provisional.replace(output)
        return output
