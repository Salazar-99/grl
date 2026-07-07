"""Compute configuration tests."""

import pytest

from grl_config.infra import ComputeConfig
from grl_config.providers import AWSProvider, get_provider, provider_for_cluster_type


def test_compute_config_defaults():
    compute = ComputeConfig()
    assert compute.ray.instance_type == "m5.4xlarge"
    assert compute.environments.instance_type == "c5.metal"


def test_aws_provider_gpus_per_instance_lookup():
    provider = AWSProvider()
    assert provider.lookup_gpus_per_instance("g5.12xlarge") == 4
    assert provider.lookup_gpus_per_instance("g5.xlarge") == 1
    assert provider.lookup_gpus_per_instance("unknown.type") is None


def test_provider_registry():
    assert get_provider("aws") is not None
    assert get_provider("aws").name == "aws"
    assert provider_for_cluster_type("EKS") is get_provider("aws")
    assert provider_for_cluster_type("BYOK") is None


def test_resolved_gpus_per_node_eks_lookup():
    compute = ComputeConfig.model_validate(
        {"rollouts": {"instance_type": "g5.12xlarge", "nodes": 1, "disk_size": 20}}
    )
    assert compute.resolved_gpus_per_node("rollouts", "EKS") == 4


def test_resolved_gpus_per_node_explicit_override():
    compute = ComputeConfig.model_validate(
        {
            "rollouts": {
                "instance_type": "g5.xlarge",
                "nodes": 1,
                "disk_size": 20,
                "gpus_per_node": 2,
            }
        }
    )
    assert compute.resolved_gpus_per_node("rollouts", "EKS") == 2


def test_unknown_instance_type_raises_on_eks():
    compute = ComputeConfig.model_validate(
        {"rollouts": {"instance_type": "custom.gpu", "nodes": 1, "disk_size": 20}}
    )
    with pytest.raises(ValueError, match="unknown aws GPU instance type"):
        compute.resolved_gpus_per_node("rollouts", "EKS")


def test_byok_requires_explicit_gpus_per_node():
    compute = ComputeConfig.model_validate(
        {"rollouts": {"instance_type": "g5.xlarge", "nodes": 1, "disk_size": 20}}
    )
    with pytest.raises(ValueError, match="no provider SKU lookup configured"):
        compute.resolved_gpus_per_node("rollouts", "BYOK")


def test_byok_explicit_gpus_per_node():
    compute = ComputeConfig.model_validate(
        {
            "rollouts": {
                "instance_type": "g5.xlarge",
                "nodes": 1,
                "disk_size": 20,
                "gpus_per_node": 1,
            }
        }
    )
    assert compute.resolved_gpus_per_node("rollouts", "BYOK") == 1


def test_aws_provider_requires_bare_metal_environments():
    provider = AWSProvider()
    with pytest.raises(ValueError, match="bare-metal"):
        provider.validate_instance_type("environments", "c6i.4xlarge")


def test_compute_validate_with_aws_provider():
    compute = ComputeConfig.model_validate(
        {
            "environments": {
                "instance_type": "c6i.4xlarge",
                "nodes": 1,
                "disk_size": 20,
            }
        }
    )
    with pytest.raises(ValueError, match="bare-metal"):
        compute.validate_with_provider(AWSProvider())


def test_node_groups_terraform_value():
    compute = ComputeConfig.model_validate(
        {
            "training": {
                "instance_type": "g5.12xlarge",
                "nodes": 2,
                "disk_size": 300,
            }
        }
    )
    node_groups = compute.node_groups_terraform_value("EKS")
    assert node_groups["training"]["instance_types"] == ["g5.12xlarge"]
    assert node_groups["training"]["node_count"] == 2
    assert node_groups["training"]["ami_type"] == "AL2023_x86_64_NVIDIA"


def test_gpu_capacity():
    compute = ComputeConfig.model_validate(
        {"rollouts": {"instance_type": "g5.12xlarge", "nodes": 2, "disk_size": 20}}
    )
    assert compute.gpu_capacity("rollouts", "EKS") == 8
