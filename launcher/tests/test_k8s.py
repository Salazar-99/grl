import subprocess
from pathlib import Path

import pytest

from grl.config import GRLConfig
from grl.k8s import helm_upgrade, load_kube_client, update_eks_kubeconfig


def test_helm_upgrade_passes_kubeconfig(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_run_tool(helm_bin, args, *, dry_run=False):
        captured["helm_bin"] = helm_bin
        captured["args"] = args
        captured["dry_run"] = dry_run

    monkeypatch.setattr("grl.k8s.run_tool", fake_run_tool)

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    chart = tmp_path / "chart"
    chart.mkdir()
    values = tmp_path / "values.yaml"
    values.write_text("key: value\n")

    helm_upgrade(
        Path("helm"),
        "grl-resources",
        chart,
        "default",
        [values],
        kubeconfig=kubeconfig,
    )

    assert captured["args"] == [
        "upgrade",
        "--install",
        "grl-resources",
        str(chart),
        "--namespace",
        "default",
        "--create-namespace",
        "--kubeconfig",
        str(kubeconfig),
        "-f",
        str(values),
    ]


def test_load_kube_client_uses_explicit_kubeconfig(monkeypatch, tmp_path: Path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    captured: dict[str, str | None] = {"config_file": None}

    def fake_load_kube_config(*, config_file=None, context=None):
        captured["config_file"] = config_file

    class FakeApiClient:
        pass

    monkeypatch.setattr("grl.k8s.config.load_kube_config", fake_load_kube_config)
    monkeypatch.setattr("grl.k8s.client.ApiClient", FakeApiClient)

    client = load_kube_client(kubeconfig=kubeconfig)
    assert isinstance(client, FakeApiClient)
    assert captured["config_file"] == str(kubeconfig)


def test_load_kube_client_missing_kubeconfig_raises(tmp_path: Path):
    with pytest.raises(Exception, match="kubeconfig not found"):
        load_kube_client(kubeconfig=tmp_path / "missing")


def test_update_eks_kubeconfig_runs_aws_command(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_which(name: str):
        return "/usr/bin/aws" if name == "aws" else None

    def fake_run(args, *, check, capture_output, text):
        captured["args"] = args
        captured["check"] = check
        return subprocess.CompletedProcess(args, 0, "Added new context\n", "")

    monkeypatch.setattr("grl.k8s.shutil.which", fake_which)
    monkeypatch.setattr("grl.k8s.subprocess.run", fake_run)

    kubeconfig = tmp_path / "config"
    path = update_eks_kubeconfig("grl", "us-west-2", kubeconfig=kubeconfig)

    assert path == kubeconfig
    assert captured["args"] == [
        "/usr/bin/aws",
        "eks",
        "update-kubeconfig",
        "--region",
        "us-west-2",
        "--name",
        "grl",
        "--kubeconfig",
        str(kubeconfig),
    ]


def test_update_eks_kubeconfig_requires_aws_cli(monkeypatch):
    monkeypatch.setattr("grl.k8s.shutil.which", lambda name: None)
    with pytest.raises(Exception, match="aws CLI not found"):
        update_eks_kubeconfig("grl", "us-west-2")
