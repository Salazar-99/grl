"""Schema validation smoke tests."""

from grl_config.model import local_model_path
from grl_config.run_id import new_run_id
from grl_config.training import GRLConfig
import pytest


def test_default_grl_config():
    config = GRLConfig.model_validate({"model": "org/model"})
    assert config.grpo.num_rollouts == 8
    assert config.pipeline.max_train_steps is None
    assert config.checkpoint.bucket_uri is None
    assert config.checkpoint.interval_steps is None
    assert str(config.checkpoint.staging_dir) == "/tmp/grl-checkpoints"
    assert config.checkpoint.max_background_uploads == 1
    assert config.environment.server_addr == "localhost:50051"
    assert config.workers.num_rollout_workers is None
    assert config.rollout.tensor_parallel_size == 1


def test_pipeline_max_train_steps_accepts_positive_value():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "pipeline": {"max_train_steps": 10},
            "checkpoint": {"bucket_uri": "s3://bucket/checkpoints"},
        }
    )
    assert config.pipeline.max_train_steps == 10


@pytest.mark.parametrize(
    "data",
    [
        {"model": "org/model", "pipeline": {"max_train_steps": 10}},
        {"model": "org/model", "checkpoint": {"interval_steps": 10}},
    ],
)
def test_checkpointing_requires_checkpoint_bucket_uri(data):
    with pytest.raises(ValueError, match="checkpoint.bucket_uri"):
        GRLConfig.model_validate(data)


def test_periodic_checkpoint_config_accepts_positive_values():
    config = GRLConfig.model_validate(
        {
            "model": "org/model",
            "checkpoint": {
                "bucket_uri": "s3://bucket/checkpoints",
                "interval_steps": 5,
                "staging_dir": "/mnt/checkpoints",
                "max_background_uploads": 2,
            },
        }
    )
    assert config.checkpoint.interval_steps == 5
    assert str(config.checkpoint.staging_dir) == "/mnt/checkpoints"
    assert config.checkpoint.max_background_uploads == 2


def test_periodic_checkpoint_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        GRLConfig.model_validate(
            {
                "model": "org/model",
                "checkpoint": {
                    "bucket_uri": "s3://bucket/checkpoints",
                    "interval_steps": 0,
                },
            }
        )


def test_local_model_path():
    assert local_model_path("Qwen/Qwen3.5-4B").name == "Qwen3.5-4B"


def test_new_run_id_format():
    run_id = new_run_id()
    assert run_id.startswith("grl-")
