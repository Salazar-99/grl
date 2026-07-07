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
        if (parent / "infra" / "modules").is_dir() and (parent / "training").is_dir():
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
    if config_dir is not None:
        candidate = Path(config_dir)
        if not candidate.is_absolute() and root is not None:
            candidate = root / candidate
        if candidate.is_dir():
            return candidate
    if root is not None:
        return root / "infra" / "aws"
    if config_dir is not None:
        candidate = Path(config_dir)
        if candidate.is_dir():
            return candidate
    packaged = package_data_root() / "infra" / "aws"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        "Terraform directory not found. Run from a GRL checkout or install the grl package."
    )


def byok_terraform_dir(config_dir: str | Path | None = None) -> Path:
    root = repo_root()
    if config_dir is not None:
        candidate = Path(config_dir)
        if not candidate.is_absolute() and root is not None:
            candidate = root / candidate
        if candidate.is_dir():
            return candidate
    if root is not None:
        return root / "infra" / "byok"
    packaged = package_data_root() / "infra" / "byok"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        "BYOK Terraform directory not found. Run from a GRL checkout or install the grl package."
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


def env_chart_path(config_path: str | Path | None = None) -> Path:
    """Path to the launcher-owned ``environments`` chart (bundle-sync DaemonSet).

    Applied by the launcher during the ENVS layer only — deliberately outside
    ``infra/modules`` since it is not a Terraform module.
    """
    root = repo_root()
    if root is not None:
        return root / "infra" / "charts" / "environments"
    if config_path is not None:
        candidate = Path(config_path).parent / "infra" / "charts" / "environments"
        if candidate.is_dir():
            return candidate
    packaged = package_data_root() / "environments-chart"
    if packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        "environments chart not found. Run from a GRL checkout or install the grl package."
    )


def grl_home() -> Path:
    """Root directory for local GRL state (runs, cached tools, etc.)."""
    return Path(os.environ.get("GRL_HOME", Path.home() / ".grl"))


def state_dir(run_id: str) -> Path:
    base = Path(os.environ.get("GRL_STATE_DIR", grl_home() / "runs"))
    path = base / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path
