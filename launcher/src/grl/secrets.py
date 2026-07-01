"""Resolve secret placeholders from environment variables."""

from __future__ import annotations

import os
import re

ENV_REF_PATTERN = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_env_ref(value: str) -> str:
    """Expand ``${env:VAR_NAME}`` to the corresponding environment value."""
    match = ENV_REF_PATTERN.match(value.strip())
    if not match:
        return value
    var_name = match.group(1)
    env_value = os.environ.get(var_name)
    if env_value is None:
        raise ValueError(f"environment variable {var_name!r} is not set")
    return env_value


def resolve_secret_fields(data: object) -> object:
    """Recursively resolve ``${env:...}`` strings in config dicts."""
    if isinstance(data, dict):
        return {key: resolve_secret_fields(value) for key, value in data.items()}
    if isinstance(data, list):
        return [resolve_secret_fields(item) for item in data]
    if isinstance(data, str):
        return resolve_env_ref(data)
    return data
