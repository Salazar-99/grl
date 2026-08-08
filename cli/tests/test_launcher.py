from contextlib import nullcontext as _nullcontext
import os
from pathlib import Path

import pytest

from grl.config import GRLConfig, ResolvedImages
from grl.k8s import rayjob_manifest, training_entrypoint


@pytest.fixture(autouse=True)
def _isolated_grl_state(monkeypatch, tmp_path: Path):
    """Launch tests must not consult or write a developer's ~/.grl registry."""
    monkeypatch.setenv("GRL_HOME", str(tmp_path / "grl-home"))


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
    monkeypatch.setattr(launcher_module, "register_cluster", lambda *a, **k: None)
    monkeypatch.setattr(launcher_module, "update_eks_kubeconfig", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher_module, "activate_environment", lambda *a, **k: calls.append("activate")
    )
    monkeypatch.setattr(
        launcher_module, "update_manager_run_id", lambda *a, **k: calls.append("manager")
    )
    monkeypatch.setattr(
        launcher_module,
        "wait_for_model_cache",
        lambda *a, **k: calls.append("model_cache"),
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
        launcher_module, "assert_envs_present", lambda *a, **k: calls.append("assert_envs")
    )
    monkeypatch.setattr(
        launcher_module,
        "manager_verify_session",
        lambda *a, **k: _nullcontext("localhost:50051"),
    )
    monkeypatch.setattr(launcher_module, "wait_for_manager_catalog", lambda *a, **k: 3)


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
    assert manifest["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "grl"
    assert manifest["spec"]["clusterSelector"] == {"ray.io/cluster": "grl-ray"}
    assert manifest["spec"]["submissionMode"] == "K8sJobMode"
    assert manifest["spec"]["metadata"] == {"app.kubernetes.io/managed-by": "grl"}
    assert "rayClusterName" not in manifest["spec"]
    assert "submitMode" not in manifest["spec"]
    assert "labels" not in manifest["spec"]["metadata"]


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
    assert not (Path(os.environ["GRL_HOME"]) / "runs").exists()


def test_dry_run_full_launch_does_not_create_a_run_record(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e"},
            "launch": {"dry_run": True},
        }
    )

    launcher_module.launch(config)

    assert calls == ["apply_infra", "manager", "activate", "model_cache", "submit"]
    assert not (Path(os.environ["GRL_HOME"]) / "runs").exists()


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


def test_load_cluster_client_eks_auto_kubeconfig_false_uses_default(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"infra": {"auto_kubeconfig": False}}}
    )
    captured: dict[str, object] = {"called": False}

    def fake_load_kube_client(**kwargs):
        captured["called"] = True
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(launcher_module, "load_kube_client", fake_load_kube_client)
    launcher_module.load_cluster_client(config)
    assert captured["called"] is True
    assert captured == {"called": True}


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
    assert calls == ["assert_envs", "manager", "model_cache", "submit"]


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
    assert calls == ["assert_resources", "manager", "activate"]


def test_cluster_only_applies_infra_and_stops(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "CLUSTER"}}
    )
    launcher_module.launch(config)
    assert calls == ["apply_infra"]


def test_cluster_only_updates_kubeconfig_after_eks_apply(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    monkeypatch.setattr(
        launcher_module,
        "register_cluster",
        lambda *a, **k: calls.append("register_cluster"),
    )
    monkeypatch.setattr(
        launcher_module,
        "update_eks_kubeconfig",
        lambda *a, **k: calls.append("update_kubeconfig"),
    )
    config = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "CLUSTER"}}
    )
    launcher_module.launch(config)
    assert calls == [
        "register_cluster",
        "apply_infra",
        "register_cluster",
        "update_kubeconfig",
    ]


def test_full_launch_runs_all_layers(monkeypatch):
    from grl import launcher as launcher_module

    calls: list[str] = []
    _stub_launch_prelude(monkeypatch, launcher_module, calls)
    config = GRLConfig.model_validate(
        {"model": "org/model", "environment": {"bundle_uri": "s3://b/e"}}
    )
    launcher_module.launch(config)
    # FULL: single EKS apply (CLUSTER step) covers resources, then envs, then training.
    # The model-cache gate must land before submit: on a cold cluster the weights
    # are still downloading when the layers above finish.
    assert calls == ["apply_infra", "manager", "activate", "model_cache", "submit"]


def test_wait_for_model_cache_blocks_until_daemonset_ready(monkeypatch):
    from grl import launcher as launcher_module

    waited: list[dict] = []
    monkeypatch.setattr(launcher_module, "daemonset_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher_module,
        "wait_for_rollout",
        lambda api, name, namespace, **kwargs: waited.append(
            {"name": name, "namespace": namespace, **kwargs}
        ),
    )
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "infra": {
                "model_cache": {
                    "tag": "org/model",
                    "namespace": "models",
                    "ready_timeout_secs": 900,
                }
            },
        }
    )

    launcher_module.wait_for_model_cache(config, object())

    assert waited == [
        {"name": "model-cache", "namespace": "models", "timeout_secs": 900}
    ]


def test_wait_for_model_cache_skips_when_cache_absent(monkeypatch):
    """Weights baked into the image: no DaemonSet, nothing to wait for."""
    from grl import launcher as launcher_module

    monkeypatch.setattr(launcher_module, "daemonset_exists", lambda *a, **k: False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not wait on a rollout that does not exist")

    monkeypatch.setattr(launcher_module, "wait_for_rollout", fail_if_called)
    config = GRLConfig.model_validate({"model": "org/model"})

    launcher_module.wait_for_model_cache(config, object())


def test_wait_for_model_cache_dry_run_skips_cluster_calls(monkeypatch):
    from grl import launcher as launcher_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not call cluster APIs in dry-run")

    monkeypatch.setattr(launcher_module, "daemonset_exists", fail_if_called)
    monkeypatch.setattr(launcher_module, "wait_for_rollout", fail_if_called)
    config = GRLConfig.model_validate({"model": "org/model"})

    launcher_module.wait_for_model_cache(config, None, dry_run=True)


def test_manager_run_overlay_and_rollout_use_only_the_resolved_run_id(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate({"model": "org/model"})
    calls: list[tuple] = []
    monkeypatch.setattr(
        launcher_module,
        "helm_upgrade",
        lambda *args, **kwargs: calls.append(("helm", args, kwargs)),
    )
    monkeypatch.setattr(
        launcher_module,
        "wait_for_rollout",
        lambda *args, **kwargs: calls.append(("rollout", args, kwargs)),
    )

    launcher_module.update_manager_run_id(
        config, {"helm": Path("helm")}, object(), "run-resolved"
    )

    overlay = Path(os.environ["GRL_HOME"]) / "runs" / "run-resolved" / "manager-run-overlay.yaml"
    assert overlay.read_text() == "manager:\n  runId: run-resolved\n"
    assert calls[0][0] == "helm"
    assert calls[0][2]["reuse_values"] is True
    assert calls[1][0] == "rollout"


def test_active_run_is_rejected_before_manager_overlay(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate({"model": "org/model"})
    monkeypatch.setattr(
        launcher_module,
        "active_rayjobs",
        lambda *args: [{"metadata": {"name": "grl-run-active"}}],
    )

    with pytest.raises(launcher_module.GrlError, match="--replace-active"):
        launcher_module.replace_active_runs(config, object())


def test_replacing_active_run_waits_for_termination(monkeypatch):
    from grl import launcher as launcher_module

    config = GRLConfig.model_validate({"model": "org/model", "launch": {"job": {"force": True}}})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        launcher_module,
        "active_rayjobs",
        lambda *args: [{"metadata": {"name": "grl-run-active"}}],
    )
    monkeypatch.setattr(
        launcher_module,
        "delete_rayjob",
        lambda _api, name, _namespace: calls.append(("delete", name)),
    )
    monkeypatch.setattr(
        launcher_module,
        "wait_for_rayjob_deletion",
        lambda _api, name, _namespace: calls.append(("wait", name)),
    )

    launcher_module.replace_active_runs(config, object())
    assert calls == [("delete", "grl-run-active"), ("wait", "grl-run-active")]
