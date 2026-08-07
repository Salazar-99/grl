"""Local, redacted records for runs submitted by this launcher."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from grl.paths import grl_home, state_dir


RUN_STATES = {
    "DEPLOYING_CLUSTER", "DEPLOYING_RESOURCES", "ACTIVATING_ENVIRONMENT",
    "QUEUED", "TRAINING", "DONE", "FAILED", "CANCELLED",
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class RunRecord:
    run_id: str
    cluster_name: str
    state: str = "DEPLOYING_CLUSTER"
    rayjob_name: str | None = None
    namespace: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    failure_reason: str | None = None
    source_config_path: str | None = None
    source_config_hash: str | None = None
    effective_config_hash: str | None = None
    resolved_images: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunRecord":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


def record_path(run_id: str) -> Path:
    return state_dir(run_id) / "run.json"


def save(record: RunRecord) -> None:
    record.updated_at = _now()
    record_path(record.run_id).write_text(json.dumps(asdict(record), indent=2))


def load(run_id: str) -> RunRecord | None:
    path = record_path(run_id)
    return RunRecord.from_dict(json.loads(path.read_text())) if path.is_file() else None


def list_runs() -> list[RunRecord]:
    base = Path(os.environ.get("GRL_STATE_DIR", grl_home() / "runs"))
    if not base.is_dir():
        return []
    records = []
    for path in base.iterdir():
        metadata = path / "run.json"
        if metadata.is_file():
            records.append(RunRecord.from_dict(json.loads(metadata.read_text())))
    return sorted(records, key=lambda item: item.created_at, reverse=True)


def write_config(run_id: str, config: Any) -> None:
    """Persist the resolved effective config; never serialize credentials/files."""
    payload = config.model_dump(mode="json")
    launch_infra = payload.get("launch", {}).get("infra", {})
    if launch_infra.get("kubeconfig"):
        launch_infra["kubeconfig"] = "<redacted>"
    # Values loaded from secret references are deliberately not copied to a run.
    def redact(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: redact(v, k) for k, v in value.items()}
        if any(token in key.lower() for token in ("secret", "token", "password", "credential", "access_key")):
            return "<redacted>"
        return value
    (state_dir(run_id) / "config.yaml").write_text(yaml.safe_dump(redact(payload), sort_keys=False))


def format_table(records: list[RunRecord]) -> str:
    if not records:
        return "No runs recorded."
    headers = ("RUN_ID", "CLUSTER", "STATE", "RAYJOB", "UPDATED")
    rows = [(r.run_id, r.cluster_name, r.state, r.rayjob_name or "-", r.updated_at) for r in records]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    return "\n".join(["  ".join(v.ljust(widths[i]) for i, v in enumerate(headers)),
                       "  ".join("-" * width for width in widths),
                       *("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)) for row in rows)])
