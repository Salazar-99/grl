import os

import pytest

from grl.config import GRLConfig, load_config
from grl.images import resolve_custom, resolve_published
from grl.secrets import resolve_env_ref, resolve_secret_fields


def test_load_example_config():
    config = load_config("example-config.yaml")
    assert config.model == "Qwen/Qwen3.5-4B"
    assert config.launch.job.backend == "rayjob"
    assert config.images.mode == "published"


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
    assert payload["telemetry"]["run_id"] == "grl-test"


def test_helm_values_overlay_includes_images():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "environment": {"bundle_uri": "s3://b/e", "id": "env-a"},
            "infra": {"vm_image_cache": {"bucket": "my-bucket"}},
        }
    )
    overlay = config.helm_values_overlay()
    assert overlay["manager"]["bundleUri"] == "s3://b/e"
    assert overlay["manager"]["envId"] == "env-a"
    assert overlay["vmImageCache"]["bucket"] == "my-bucket"


def test_terraform_vars_from_resolved_images():
    from grl.config import ResolvedImages

    config = GRLConfig.model_validate({"model": "org/model"})
    resolved = ResolvedImages(
        head="reg/training-head:1",
        rollouts="reg/training-rollouts:1",
        training="reg/training-training:1",
        manager="reg/manager:1",
    )
    vars_ = config.terraform_vars(resolved)
    assert vars_["ray_head_image"] == "reg/training-head:1"
    assert vars_["manager_image"] == "reg/manager:1"


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
