"""Model id → on-disk cache path mapping."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL_CACHE_ROOT = "/models"


def model_cache_root() -> str:
    return os.environ.get("GRL_MODEL_CACHE_ROOT", DEFAULT_MODEL_CACHE_ROOT)


def local_model_path(model_id: str, *, cache_root: str | None = None) -> Path:
    """Map ``Qwen/Qwen3.5-4B`` → ``/models/Qwen3.5-4B`` on cluster nodes."""
    root = cache_root or model_cache_root()
    repo_name = model_id.rsplit("/", 1)[-1]
    return Path(root) / repo_name
