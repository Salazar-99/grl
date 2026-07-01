"""Schema validation smoke tests."""

from grl_config.model import local_model_path
from grl_config.run_id import new_run_id
from grl_config.training import GRLConfig


def test_default_grl_config():
    config = GRLConfig.model_validate({"model": "org/model"})
    assert config.grpo.num_rollouts == 8
    assert config.environment.server_addr == "localhost:50051"


def test_local_model_path():
    assert local_model_path("Qwen/Qwen3.5-4B").name == "Qwen3.5-4B"


def test_new_run_id_format():
    run_id = new_run_id()
    assert run_id.startswith("grl-")
