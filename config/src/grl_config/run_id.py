"""Run identity helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def new_run_id() -> str:
    """A fresh human-readable run id, e.g. ``grl-20260613-141503-9f3a1c``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"grl-{stamp}-{uuid.uuid4().hex[:6]}"
