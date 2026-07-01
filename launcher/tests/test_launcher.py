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
