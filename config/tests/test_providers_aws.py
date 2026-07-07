"""AWS cloud provider tests."""

from grl_config.providers.aws import AWSProvider


def test_default_ami_types():
    provider = AWSProvider()
    assert provider.default_ami_type("rollouts") == "AL2023_x86_64_NVIDIA"
    assert provider.default_ami_type("ray") == "AL2023_x86_64_STANDARD"
