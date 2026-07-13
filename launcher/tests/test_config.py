"""Launcher config tests."""

import os

import pytest
from pydantic import ValidationError

from grl.config import GRLConfig, load_config, resolve_env_ref, resolve_secret_fields
from grl.images import resolve_custom, resolve_published
from grl.launcher import CapacityError, validate_capacity


def test_load_example_config():
    config = load_config("example-config.yaml")
    assert config.model == "Qwen/Qwen3.5-4B"
    assert config.launch.job.backend == "rayjob"
    assert config.images.mode == "published"
    assert config.compute.rollouts.instance_type == "g5.xlarge"


def test_resolve_env_ref():
    os.environ["GRL_TEST_SECRET"] = "secret-value"
    assert resolve_env_ref("${env:GRL_TEST_SECRET}") == "secret-value"


def test_resolve_secret_fields_nested():
    os.environ["GRL_TEST_NESTED"] = "nested"
    data = {"infra": {"password": "${env:GRL_TEST_NESTED}"}}
    resolved = resolve_secret_fields(data)
    assert resolved["infra"]["password"] == "nested"


def test_training_payload_excludes_infra():
    config = GRLConfig.model_validate({"model": "org/model"})
    payload = config.training_payload(run_id="grl-test")
    assert "infra" not in payload
    assert "launch" not in payload
    assert "compute" not in payload
    assert payload["telemetry"]["run_id"] == "grl-test"
    assert payload["workers"]["num_rollout_workers"] == 1


def test_training_payload_derives_rollout_workers():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "compute": {
                "rollouts": {
                    "instance_type": "g5.12xlarge",
                    "nodes": 2,
                    "disk_size": 20,
                }
            },
        }
    )
    payload = config.training_payload(run_id="grl-test")
    assert payload["workers"]["num_rollout_workers"] == 8


def test_helm_values_overlay_excludes_bundle_uri():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e", "id": "env-a"},
            "infra": {"vm_image_cache": {"bucket": "my-bucket"}},
        }
    )
    overlay = config.helm_values_overlay()
    assert "bundleUri" not in overlay["manager"]
    assert overlay["manager"]["envId"] == "env-a"
    assert overlay["vmImageCache"]["bucket"] == "my-bucket"
    assert overlay["vmImageCache"]["scratchGb"] == 2
    assert overlay["rayCluster"]["workers"]["rollouts"]["replicas"] == 1
    assert overlay["rayCluster"]["workers"]["rollouts"]["gpusPerNode"] == 1


def test_helm_values_overlay_scratch_gb_override():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e", "id": "env-a"},
            "infra": {"vm_image_cache": {"bucket": "my-bucket", "scratch_gb": 4}},
        }
    )
    assert config.helm_values_overlay()["vmImageCache"]["scratchGb"] == 4


def test_bootstrap_key_flows_to_helm_and_terraform():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "infra": {
                "vm_image_cache": {
                    "bucket": "my-bucket",
                    "bootstrap_key": "bootstrap/grl-bootstrap-abc.cpio.gz",
                }
            },
        }
    )
    cache = config.helm_values_overlay()["vmImageCache"]
    assert cache["bootstrapKey"].endswith(".cpio.gz")


def test_env_helm_values_carries_bundle():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e", "id": "env-a"},
        }
    )
    values = config.env_helm_values()
    assert values["bundleUri"] == "s3://b/e"
    assert values["envId"] == "env-a"
    assert values["activeDir"] == "active"
    assert values["hostPath"] == "/var/lib/grl"


def test_terraform_vars_from_resolved_images():
    from grl.config import ResolvedImages

    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "compute": {
                "training": {
                    "instance_type": "g5.12xlarge",
                    "nodes": 2,
                    "disk_size": 300,
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
    vars_ = config.terraform_vars(resolved)
    assert vars_["ray_head_image"] == "reg/training-head:1"
    assert vars_["manager_image"] == "reg/manager:1"
    assert vars_["node_groups"]["training"]["instance_types"] == ["g5.12xlarge"]
    assert vars_["node_groups"]["environments"]["instance_types"] == ["c5.metal"]
    assert vars_["ray_training_gpus_per_node"] == 4
    assert vars_["ray_training_replicas"] == 2


def test_resolve_published_images():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "images": {
                "mode": "published",
                "registry": "ghcr.io/example/grl",
                "tag": "0.2.0",
            },
        }
    )
    resolved = resolve_published(config)
    assert resolved.head == "ghcr.io/example/grl-training-head:0.2.0"
    assert resolved.manager == "ghcr.io/example/grl-manager:0.2.0"


def test_grpo_has_loss_scale_factor():
    config = GRLConfig.model_validate({"model": "org/model", "grpo": {"loss_scale_factor": 4}})
    assert config.grpo.loss_scale_factor == 4


def test_training_payload_validates_under_shared_training_config():
    from grl_config.training import GRLConfig as TrainingGRLConfig

    config = load_config("example-config.yaml")
    payload = config.training_payload(run_id="grl-contract-test")
    validated = TrainingGRLConfig.model_validate(payload)
    assert validated.model == config.model
    assert validated.telemetry.run_id == "grl-contract-test"


def _launch(deployment_type):
    return GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": deployment_type}}
    ).launch


def test_deployment_type_layers_and_prereqs():
    full = _launch("FULL")
    assert full.runs_cluster()
    assert full.runs_resources()
    assert full.runs_envs()
    assert full.runs_training()
    assert full.required_present_layer() is None

    cluster = _launch("CLUSTER")
    assert cluster.runs_cluster() and not cluster.runs_resources()
    assert cluster.required_present_layer() is None

    resources = _launch("RESOURCES")
    assert resources.runs_resources() and not resources.runs_envs()
    assert resources.required_present_layer() == "CLUSTER"

    envs = _launch("ENVS")
    assert envs.runs_envs() and not envs.runs_training()
    assert envs.required_present_layer() == "RESOURCES"

    training = _launch("TRAINING")
    assert training.runs_training() and not training.runs_cluster()
    assert training.required_present_layer() == "ENVS"


def test_invalid_deployment_type_lists_options():
    with pytest.raises(ValidationError) as exc:
        GRLConfig.model_validate(
            {"model": "org/model", "launch": {"deployment_type": "BOGUS"}}
        )
    message = str(exc.value)
    assert "valid options" in message
    assert "TRAINING" in message


def test_invalid_cluster_type_lists_options():
    with pytest.raises(ValidationError) as exc:
        GRLConfig.model_validate(
            {"model": "org/model", "launch": {"cluster_type": "GKE"}}
        )
    assert "EKS" in str(exc.value)


def test_byok_requires_kubeconfig():
    with pytest.raises(ValidationError):
        GRLConfig.model_validate(
            {"model": "org/model", "launch": {"cluster_type": "BYOK"}}
        )


def test_byok_kubeconfig_parsing(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {
                "cluster_type": "BYOK",
                "infra": {"kubeconfig": str(kubeconfig)},
            },
            "compute": {
                "rollouts": {
                    "instance_type": "g5.xlarge",
                    "nodes": 1,
                    "disk_size": 20,
                    "gpus_per_node": 1,
                },
                "training": {
                    "instance_type": "g5.xlarge",
                    "nodes": 1,
                    "disk_size": 20,
                    "gpus_per_node": 1,
                },
            },
        }
    )
    assert config.launch.infra.resolved_kubeconfig() == kubeconfig
    assert config.launch.is_byok() is True
    assert config.launch.infra.uses_kubeconfig() is True


def test_deploy_workloads_gated_by_resources_layer():
    from grl.config import ResolvedImages

    resolved = ResolvedImages(head="h", rollouts="r", training="t", manager="m")
    cluster = GRLConfig.model_validate(
        {"model": "org/model", "launch": {"deployment_type": "CLUSTER"}}
    )
    assert cluster.terraform_vars(resolved)["deploy_workloads"] is False
    full = GRLConfig.model_validate({"model": "org/model"})
    assert full.terraform_vars(resolved)["deploy_workloads"] is True


def test_byok_terraform_vars_include_kubeconfig(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    from grl.config import ResolvedImages

    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "launch": {"infra": {"kubeconfig": str(kubeconfig)}},
            "compute": {
                "rollouts": {
                    "instance_type": "g5.xlarge",
                    "nodes": 1,
                    "disk_size": 20,
                    "gpus_per_node": 1,
                },
                "training": {
                    "instance_type": "g5.xlarge",
                    "nodes": 1,
                    "disk_size": 20,
                    "gpus_per_node": 1,
                },
            },
        }
    )
    resolved = ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )
    vars_ = config.byok_terraform_vars(resolved)
    assert vars_["kubeconfig_path"] == str(kubeconfig)
    assert vars_["ray_rollouts_gpus_per_node"] == 1


def test_validate_capacity_warns_on_env_admission(capsys):
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "compute": {
                "rollouts": {
                    "instance_type": "g5.12xlarge",
                    "nodes": 2,
                    "disk_size": 20,
                },
                "environments": {
                    "instance_type": "c5.metal",
                    "nodes": 1,
                    "disk_size": 20,
                },
            },
            "rollout": {"max_concurrent_trajectories": 32},
        }
    )
    warnings = validate_capacity(config)
    assert warnings
    assert "environment admission" in warnings[0]


def test_validate_capacity_rejects_excess_workers():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "workers": {"num_rollout_workers": 99},
            "compute": {
                "rollouts": {
                    "instance_type": "g5.xlarge",
                    "nodes": 1,
                    "disk_size": 20,
                }
            },
        }
    )
    with pytest.raises(CapacityError, match="exceeds rollout GPU capacity"):
        validate_capacity(config)
