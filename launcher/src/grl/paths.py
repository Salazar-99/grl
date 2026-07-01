"""Resolve packaged assets and repository paths."""

from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path


def repo_root() -> Path | None:
    """Return the GRL repository root when running from a checkout."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "infra" / "main.tf").is_file() and (parent / "training").is_dir():
            return parent
    env_root = os.environ.get("GRL_REPO_ROOT")
    if env_root:
        path = Path(env_root)
        if path.is_dir():
            return path
    return None


@lru_cache
def package_data_root() -> Path:
    """Directory containing packaged Terraform and Helm assets."""
    try:
        return Path(str(resources.files("grl") / "data"))
    except (ModuleNotFoundError, TypeError):
        return Path(__file__).resolve().parent / "data"


def terraform_dir(config_dir: str | Path | None = None) -> Path:
    root = repo_root()
    if root is not None:
        return root / "infra"
    if config_dir is not None:
        candidate = Path(config_dir) / "infra"
        if candidate.is_dir():
            return candidate
    packaged = package_data_root() / "infra"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        "Terraform directory not found. Run from a GRL checkout or install the grl package."
    )


def helm_chart_path(config_path: str | Path | None = None) -> Path:
    root = repo_root()
    if root is not None:
        return root / "infra" / "modules" / "resources" / "chart"
    if config_path is not None:
        candidate = Path(config_path).parent / "infra" / "modules" / "resources" / "chart"
        if candidate.is_dir():
            return candidate
    packaged = package_data_root() / "chart"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        "Helm chart not found. Run from a GRL checkout or install the grl package."
    )


def state_dir(run_id: str) -> Path:
    base = Path(os.environ.get("GRL_STATE_DIR", Path.home() / ".cache" / "grl" / "runs"))
    path = base / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path
