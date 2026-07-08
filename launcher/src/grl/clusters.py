"""Launcher cluster registry stored under ~/.grl/terraform-state."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from grl.config import GRLConfig
from grl.paths import cluster_dir, list_cluster_dirs, terraform_state_path


@dataclass
class ClusterRecord:
    cluster_name: str
    cluster_type: str
    provider_name: str
    region: str
    release_namespace: str
    release_name: str
    status: str
    state_path: str
    kubeconfig_hash: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_run_id: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ClusterRecord:
        return cls(**{key: payload.get(key) for key in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def kubeconfig_hash(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:16]


def cluster_metadata_path(cluster_name: str) -> Path:
    return cluster_dir(cluster_name) / "cluster.json"


def load_cluster_record(cluster_name: str) -> ClusterRecord | None:
    path = cluster_metadata_path(cluster_name)
    if not path.is_file():
        return None
    return ClusterRecord.from_dict(json.loads(path.read_text()))


def save_cluster_record(record: ClusterRecord) -> None:
    path = cluster_metadata_path(record.cluster_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2))


def build_cluster_record(
    config: GRLConfig,
    *,
    run_id: str | None = None,
    status: str = "active",
) -> ClusterRecord:
    byok = config.launch.is_byok()
    state_path = terraform_state_path(config.infra.cluster_name, byok=byok)
    existing = load_cluster_record(config.infra.cluster_name)
    now = _now_iso()
    kubeconfig = (
        config.launch.infra.resolved_kubeconfig()
        if config.launch.infra.kubeconfig
        else None
    )
    return ClusterRecord(
        cluster_name=config.infra.cluster_name,
        cluster_type=config.launch.cluster_type,
        provider_name=config.infra.cluster_name if config.launch.is_eks() else "-",
        region=config.infra.region if config.launch.is_eks() else "-",
        release_namespace=config.infra.release_namespace,
        release_name=config.infra.release_name,
        status=status,
        state_path=str(state_path),
        kubeconfig_hash=kubeconfig_hash(kubeconfig),
        created_at=existing.created_at if existing else now,
        updated_at=now,
        last_run_id=run_id or (existing.last_run_id if existing else None),
    )


def register_cluster(
    config: GRLConfig,
    *,
    run_id: str | None = None,
    status: str = "active",
) -> ClusterRecord:
    record = build_cluster_record(config, run_id=run_id, status=status)
    save_cluster_record(record)
    return record


def remove_cluster(cluster_name: str) -> None:
    path = cluster_dir(cluster_name)
    if path.is_dir():
        shutil.rmtree(path)


def mark_cluster_destroyed(config: GRLConfig) -> None:
    remove_cluster(config.infra.cluster_name)


def list_clusters() -> list[ClusterRecord]:
    records: list[ClusterRecord] = []
    for cluster_path in list_cluster_dirs():
        metadata = cluster_path / "cluster.json"
        if metadata.is_file():
            record = ClusterRecord.from_dict(json.loads(metadata.read_text()))
            if record.status == "destroyed":
                shutil.rmtree(cluster_path)
                continue
            records.append(record)
            continue
        # Fall back to directory name when metadata is missing.
        records.append(
            ClusterRecord(
                cluster_name=cluster_path.name,
                cluster_type="unknown",
                provider_name="-",
                region="-",
                release_namespace="-",
                release_name="-",
                status="unknown",
                state_path=str(cluster_path),
            )
        )
    return sorted(records, key=lambda record: record.cluster_name)


def format_cluster_table(records: list[ClusterRecord]) -> str:
    if not records:
        return "No clusters registered."
    headers = ("NAME", "TYPE", "PROVIDER", "REGION", "NAMESPACE", "STATUS", "LAST_RUN")
    rows = [
        (
            record.cluster_name,
            record.cluster_type,
            record.provider_name,
            record.region,
            record.release_namespace,
            record.status,
            record.last_run_id or "-",
        )
        for record in records
    ]
    widths = [
        max(len(headers[idx]), *(len(row[idx]) for row in rows))
        for idx in range(len(headers))
    ]
    lines = [
        "  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)),
        "  ".join("-" * widths[idx] for idx in range(len(headers))),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(row))
        for row in rows
    )
    return "\n".join(lines)
