import subprocess
from pathlib import Path

import pytest

from grl.config import GRLConfig
from grl.k8s import (
    helm_upgrade,
    is_kubernetes_service_addr,
    is_loopback_addr,
    load_kube_client,
    port_forward_service,
    update_eks_kubeconfig,
)


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


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("localhost:50051", True),
        ("127.0.0.1:50051", True),
        ("grl-manager.default.svc:50051", False),
    ],
)
def test_is_loopback_addr(addr: str, expected: bool):
    assert is_loopback_addr(addr) is expected


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("grl-manager.default.svc:50051", True),
        ("grl-manager.default.svc.cluster.local:50051", True),
        ("my-env.example.com:50051", False),
        ("localhost:50051", False),
    ],
)
def test_is_kubernetes_service_addr(addr: str, expected: bool):
    assert is_kubernetes_service_addr(addr) is expected


def test_port_forward_service_runs_kubectl(monkeypatch, tmp_path: Path):
    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.stderr = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    captured: dict[str, object] = {"proc": FakeProc()}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return captured["proc"]

    monkeypatch.setattr("grl.k8s.subprocess.Popen", fake_popen)
    monkeypatch.setattr("grl.k8s._wait_for_local_port", lambda port: None)

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")

    with port_forward_service(
        Path("kubectl"),
        "grl-manager",
        "default",
        50051,
        50051,
        kubeconfig=kubeconfig,
    ) as addr:
        assert addr == "127.0.0.1:50051"

    assert captured["args"] == [
        "kubectl",
        "port-forward",
        "svc/grl-manager",
        "50051:50051",
        "-n",
        "default",
        "--kubeconfig",
        str(kubeconfig),
    ]
