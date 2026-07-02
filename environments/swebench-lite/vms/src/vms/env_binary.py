"""Build or locate the grl-env (env crate) binary for injection into base ext4."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENV_CRATE = Path(__file__).resolve().parents[3] / "env"


def rust_target(platform: str) -> str:
    if "arm64" in platform or "aarch64" in platform:
        return "aarch64-unknown-linux-gnu"
    return "x86_64-unknown-linux-gnu"


def resolve_grl_env_binary(*, platform: str) -> Path:
    """Return a linux grl-env binary, building the env crate when needed."""
    override = os.environ.get("GRL_ENV_BIN")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"GRL_ENV_BIN not found: {path}")
        return path

    target = rust_target(platform)
    release = ENV_CRATE / "target" / target / "release" / "env"
    if release.is_file():
        return release

    print(f"building grl-env for {target}...", file=sys.stderr)
    subprocess.run(
        ["cargo", "build", "--release", "--target", target],
        cwd=ENV_CRATE,
        check=True,
    )
    if not release.is_file():
        raise FileNotFoundError(
            f"grl-env build did not produce {release}; set GRL_ENV_BIN to a prebuilt binary"
        )
    return release
