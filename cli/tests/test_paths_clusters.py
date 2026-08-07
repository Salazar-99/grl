from pathlib import Path

import pytest

from grl.clusters import (
    ClusterRecord,
    format_cluster_table,
    list_clusters,
    mark_cluster_destroyed,
    register_cluster,
)
from grl.config import GRLConfig
from grl.paths import (
    cluster_dir,
    terraform_state_base,
    terraform_state_path,
    validate_cluster_name,
)


def test_validate_cluster_name_accepts_slug():
    assert validate_cluster_name("grl-dev-a100") == "grl-dev-a100"


def test_validate_cluster_name_rejects_invalid_chars():
    with pytest.raises(ValueError, match="letters, numbers"):
        validate_cluster_name("bad/name")


def test_terraform_state_path_uses_grl_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    assert terraform_state_base() == tmp_path / "terraform-state"
    assert terraform_state_path("dev-a", byok=False) == (
        tmp_path / "terraform-state" / "dev-a" / "eks" / "terraform.tfstate"
    )
    assert terraform_state_path("dev-a", byok=True) == (
        tmp_path / "terraform-state" / "dev-a" / "byok" / "terraform.tfstate"
    )


def test_different_cluster_names_have_isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    one = terraform_state_path("cluster-a", byok=False)
    two = terraform_state_path("cluster-b", byok=False)
    assert one != two


def test_register_and_list_clusters(monkeypatch, tmp_path):
    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    config = GRLConfig.model_validate(
        {"model": "org/model", "infra": {"cluster_name": "dev-a"}}
    )
    register_cluster(config, run_id="grl-test-1")

    records = list_clusters()
    assert len(records) == 1
    assert records[0].cluster_name == "dev-a"
    assert records[0].cluster_type == "EKS"
    assert records[0].status == "active"
    assert records[0].last_run_id == "grl-test-1"
    assert cluster_dir("dev-a").is_dir()


def test_mark_cluster_destroyed_removes_from_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    config = GRLConfig.model_validate(
        {"model": "org/model", "infra": {"cluster_name": "dev-a"}}
    )
    register_cluster(config, run_id="grl-test-1")
    assert len(list_clusters()) == 1

    mark_cluster_destroyed(config)

    assert list_clusters() == []
    assert not cluster_dir("dev-a").exists()


def test_list_clusters_removes_legacy_destroyed_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    config = GRLConfig.model_validate(
        {"model": "org/model", "infra": {"cluster_name": "dev-a"}}
    )
    register_cluster(config, run_id="grl-test-1")
    register_cluster(config, status="destroyed")

    assert list_clusters() == []
    assert not cluster_dir("dev-a").exists()


def test_format_cluster_table_empty():
    assert format_cluster_table([]) == "No clusters registered."


def test_format_cluster_table_renders_rows():
    table = format_cluster_table(
        [
            ClusterRecord(
                cluster_name="dev-a",
                cluster_type="EKS",
                provider_name="dev-a",
                region="us-west-2",
                release_namespace="default",
                release_name="grl-resources",
                status="active",
                state_path="/tmp/state",
                last_run_id="grl-123",
            )
        ]
    )
    assert "dev-a" in table
    assert "grl-123" in table
