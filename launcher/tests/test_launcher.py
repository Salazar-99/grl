from grl.config import GRLConfig
from grl.k8s import rayjob_manifest, training_entrypoint


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
            "launch": {
                "dry_run": True,
                "preflight_only": True,
                "infra": {"apply": False},
                "environment": {"activate": False},
                "job": {"submit": False},
            },
        }
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not call cluster APIs in dry-run preflight")

    monkeypatch.setattr(launcher_module, "verify_bundle", fail_if_called)
    result = launcher_module.launch(config)
    assert result.run_id.startswith("grl-")


def test_load_cluster_client_prefers_explicit_kubeconfig(monkeypatch, tmp_path):
    from grl import launcher as launcher_module

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {
                "infra": {
                    "kubeconfig": str(kubeconfig),
                    "auto_kubeconfig": True,
                }
            },
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


def test_apply_infra_routes_byo_cluster_to_byok_root(monkeypatch, tmp_path):
    from grl.config import ResolvedImages
    from grl import terraform as terraform_module

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {
                "infra": {
                    "apply": False,
                    "kubeconfig": str(kubeconfig),
                }
            },
        }
    )
    resolved = ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )
    calls: list[str] = []

    def fake_apply_byok_infra(*args, **kwargs):
        calls.append("byok")
        return tmp_path / "byok.auto.tfvars.json"

    def fail_full_stack(*args, **kwargs):
        raise AssertionError("full-stack apply should not run for BYO kubeconfig")

    monkeypatch.setattr(terraform_module, "apply_byok_infra", fake_apply_byok_infra)
    monkeypatch.setattr(terraform_module, "apply_full_stack_infra", fail_full_stack)

    terraform_module.apply_infra(config, resolved, tmp_path / "terraform", "grl-test")
    assert calls == ["byok"]


def test_apply_infra_routes_full_stack_when_apply_true(monkeypatch, tmp_path):
    from grl.config import ResolvedImages
    from grl import terraform as terraform_module

    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"infra": {"apply": True}},
        }
    )
    resolved = ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )
    calls: list[str] = []

    def fake_apply_full_stack_infra(*args, **kwargs):
        calls.append("full_stack")
        return tmp_path / "terraform.auto.tfvars.json"

    def fail_byok(*args, **kwargs):
        raise AssertionError("BYOK apply should not run when full-stack apply is selected")

    monkeypatch.setattr(terraform_module, "apply_full_stack_infra", fake_apply_full_stack_infra)
    monkeypatch.setattr(terraform_module, "apply_byok_infra", fail_byok)

    terraform_module.apply_infra(config, resolved, tmp_path / "terraform", "grl-test")
    assert calls == ["full_stack"]
