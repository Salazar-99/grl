from pathlib import Path

import pytest

from grl.config import GRLConfig, ResolvedImages
from grl.terraform import apply_infra, destroy_infra


def _resolved() -> ResolvedImages:
    return ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )


def test_apply_infra_passes_state_aware_args(monkeypatch, tmp_path):
    from grl import terraform as terraform_module

    monkeypatch.setenv("GRL_HOME", str(tmp_path))
    config = GRLConfig.model_validate({"model": "org/model", "infra": {"cluster_name": "dev-a"}})
    captured: dict[str, object] = {}

    def fake_apply(
        config,
        resolved,
        terraform_bin,
        run_id,
        tf_root,
        *,
        byok=False,
        dry_run=False,
    ):
        captured["state_path"] = terraform_module.terraform_state_path(
            config.infra.cluster_name,
            byok=byok,
        )
        captured["byok"] = byok
        return Path("tfvars"), captured["state_path"]

    monkeypatch.setattr(terraform_module, "_apply_terraform_root", fake_apply)
    terraform_module.apply_infra(config, _resolved(), tmp_path / "terraform", "grl-test")
    assert captured["byok"] is False
    assert captured["state_path"] == (
        tmp_path / "terraform-state" / "dev-a" / "eks" / "terraform.tfstate"
    )


def test_destroy_infra_routes_byok(monkeypatch, tmp_path):
    from grl import terraform as terraform_module

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"cluster_type": "BYOK", "infra": {"kubeconfig": str(kubeconfig)}},
            "infra": {"cluster_name": "local-byok"},
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(
        terraform_module,
        "destroy_byok_infra",
        lambda *a, **k: calls.append("byok") or Path("tfvars"),
    )
    monkeypatch.setattr(
        terraform_module,
        "destroy_full_stack_infra",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("EKS destroy should not run")),
    )

    terraform_module.destroy_infra(config, _resolved(), tmp_path / "terraform", "grl-test")
    assert calls == ["byok"]


def test_destroy_infra_routes_eks(monkeypatch, tmp_path):
    from grl import terraform as terraform_module

    config = GRLConfig.model_validate(
        {"model": "org/model", "infra": {"cluster_name": "dev-a"}}
    )
    calls: list[str] = []
    monkeypatch.setattr(
        terraform_module,
        "destroy_full_stack_infra",
        lambda *a, **k: calls.append("eks") or Path("tfvars"),
    )
    monkeypatch.setattr(
        terraform_module,
        "destroy_byok_infra",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("BYOK destroy should not run")),
    )

    terraform_module.destroy_infra(config, _resolved(), tmp_path / "terraform", "grl-test")
    assert calls == ["eks"]


def test_terraform_vars_for_teardown_force_deploy_workloads():
    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "TRAINING"}}
    )
    vars_ = config.terraform_vars_for_teardown(_resolved())
    assert vars_["deploy_workloads"] is True


def test_teardown_routes_destroy(monkeypatch, tmp_path):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate({"model": "org/model", "infra": {"cluster_name": "dev-a"}})
    calls: list[str] = []

    monkeypatch.setattr(launcher_module, "ensure_managed_tools", lambda c: {"terraform": Path("tf")})
    monkeypatch.setattr(
        launcher_module,
        "resolve_runtime_images",
        lambda c, dry_run=False: _resolved(),
    )
    monkeypatch.setattr(
        launcher_module,
        "destroy_infra",
        lambda *a, **k: calls.append("destroy"),
    )
    monkeypatch.setattr(launcher_module, "mark_cluster_destroyed", lambda c: calls.append("mark"))

    launcher_module.teardown(config, auto_yes=True)
    assert calls == ["destroy", "mark"]


def test_teardown_dry_run_skips_registry_update(monkeypatch, tmp_path):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"dry_run": True}, "infra": {"cluster_name": "dev-a"}}
    )
    calls: list[str] = []

    monkeypatch.setattr(launcher_module, "ensure_managed_tools", lambda c: {"terraform": Path("tf")})
    monkeypatch.setattr(
        launcher_module,
        "resolve_runtime_images",
        lambda c, dry_run=False: _resolved(),
    )
    monkeypatch.setattr(
        launcher_module,
        "destroy_infra",
        lambda *a, **k: calls.append("destroy"),
    )
    monkeypatch.setattr(
        launcher_module,
        "mark_cluster_destroyed",
        lambda c: (_ for _ in ()).throw(AssertionError("should not mark destroyed on dry-run")),
    )

    launcher_module.teardown(config, auto_yes=True)
    assert calls == ["destroy"]
