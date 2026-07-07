from pathlib import Path

from grl.config import GRLConfig, ResolvedImages
from grl.k8s import rayjob_manifest, training_entrypoint


def _stub_launch_prelude(monkeypatch, launcher_module, calls):
    """Patch out the image/tool/preflight prelude of launch() so gating tests
    exercise only the layer-routing logic."""
    monkeypatch.setattr(
        launcher_module,
        "ensure_managed_tools",
        lambda c: {"terraform": Path("tf"), "helm": Path("helm"), "kubectl": Path("k")},
    )
    monkeypatch.setattr(
        launcher_module,
        "resolve_runtime_images",
        lambda c, dry_run=False: ResolvedImages(head="h", rollouts="r", training="t", manager="m"),
    )
    monkeypatch.setattr(launcher_module, "run_preflight", lambda c, dry_run=False: None)
    monkeypatch.setattr(launcher_module, "load_cluster_client", lambda c: object())
    monkeypatch.setattr(launcher_module, "persist_run_metadata", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher_module, "apply_infra", lambda *a, **k: calls.append("apply_infra")
    )
    monkeypatch.setattr(
        launcher_module, "activate_environment", lambda *a, **k: calls.append("activate")
    )
    monkeypatch.setattr(
        launcher_module,
        "submit_training_job",
        lambda *a, **k: (calls.append("submit"), "grl-run-x")[1],
    )
    monkeypatch.setattr(
        launcher_module, "assert_cluster_present", lambda c, a: calls.append("assert_cluster")
    )
    monkeypatch.setattr(
        launcher_module, "assert_resources_present", lambda c, a: calls.append("assert_resources")
    )
    monkeypatch.setattr(
        launcher_module, "assert_envs_present", lambda c: calls.append("assert_envs")
    )
    monkeypatch.setattr(launcher_module, "wait_for_manager_catalog", lambda c: 3)


def test_training_entrypoint_roundtrip():
    yaml_text = "model: test\n"
    entrypoint = training_entrypoint(yaml_text)
    assert "training.main" in entrypoint
    assert entrypoint.startswith("python -c")


def test_rayjob_manifest_targets_cluster():
    manifest = rayjob_manifest(
        name="grl-run-abc",
        namespace="default",
        ray_cluster_name="grl-ray",
        entrypoint="python -m training.main",
    )
    assert manifest["kind"] == "RayJob"
    assert manifest["spec"]["rayClusterName"] == "grl-ray"
    assert manifest["spec"]["submitMode"] == "K8sJobMode"


def test_dry_run_launch_skips_cluster_calls(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"dry_run": True, "preflight_only": True},
        }
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not call cluster APIs in dry-run preflight")

    monkeypatch.setattr(launcher_module, "verify_bundle", fail_if_called)
    result = launcher_module.launch(config)
    assert result.run_id.startswith("grl-")


def test_load_cluster_client_byok_uses_kubeconfig(monkeypatch, tmp_path):
    from grl import launcher as launcher_module

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"cluster_type": "BYOK", "infra": {"kubeconfig": str(kubeconfig)}},
        }
    )
    captured: dict[str, object] = {}

    def fake_load_kube_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(launcher_module, "load_kube_client", fake_load_kube_client)
    launcher_module.load_cluster_client(config)
    assert captured["kubeconfig"] == kubeconfig
    assert "cluster_name" not in captured


def test_load_cluster_client_eks_uses_token(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate({"model": "org/model"})
    captured: dict[str, object] = {}

    def fake_load_kube_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(launcher_module, "load_kube_client", fake_load_kube_client)
    launcher_module.load_cluster_client(config)
    assert captured["cluster_name"] == "grl"
    assert "kubeconfig" not in captured


def _resolved():
    return ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )


def test_apply_infra_routes_byok_resources_to_byok_root(monkeypatch, tmp_path):
    from grl import terraform as terraform_module

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"cluster_type": "BYOK", "infra": {"kubeconfig": str(kubeconfig)}},
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(
        terraform_module, "apply_byok_infra", lambda *a, **k: calls.append("byok")
    )
    monkeypatch.setattr(
        terraform_module,
        "apply_full_stack_infra",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("EKS root should not run for BYOK")),
    )

    terraform_module.apply_infra(config, _resolved(), tmp_path / "terraform", "grl-test")
    assert calls == ["byok"]


def test_apply_infra_routes_eks_to_full_stack(monkeypatch, tmp_path):
    from grl import terraform as terraform_module

    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "FULL"}}
    )
    calls: list[str] = []
    monkeypatch.setattr(
        terraform_module, "apply_full_stack_infra", lambda *a, **k: calls.append("full_stack")
    )
    monkeypatch.setattr(
        terraform_module,
        "apply_byok_infra",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("BYOK root should not run for EKS")),
    )

    terraform_module.apply_infra(config, _resolved(), tmp_path / "terraform", "grl-test")
    assert calls == ["full_stack"]


def test_terraform_vars_deploy_workloads_false_for_cluster_layer():
    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "CLUSTER"}}
    )
    from grl import terraform as terraform_module

    tfvars = config.terraform_vars(_resolved())
    assert tfvars["deploy_workloads"] is False
    # sanity: apply_infra still routes CLUSTER through the EKS root
    del terraform_module


def test_training_only_asserts_envs_then_submits(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e"},
            "launch": {"deployment_type": "TRAINING"},
        }
    )
    launcher_module.launch(config)
    assert calls == ["assert_envs", "submit"]


def test_envs_only_asserts_resources_then_activates(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e"},
            "launch": {"deployment_type": "ENVS"},
        }
    )
    launcher_module.launch(config)
    assert calls == ["assert_resources", "activate"]


def test_cluster_only_applies_infra_and_stops(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "CLUSTER"}}
    )
    launcher_module.launch(config)
    assert calls == ["apply_infra"]


def test_full_launch_runs_all_layers(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {"model": "org/model", "environment": {"bundle_uri": "s3://b/e"}}
    )
    launcher_module.launch(config)
    # FULL: single EKS apply (CLUSTER step) covers resources, then envs, then training.
    assert calls == ["apply_infra", "activate", "submit"]
