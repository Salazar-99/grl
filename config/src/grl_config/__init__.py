"""Shared GRL configuration schema."""

from grl_config.infra import (
    ROLLOUTS_RESOURCE,
    TRAINING_RESOURCE,
    ComputeConfig,
    ComputeRoleConfig,
)
from grl_config.providers import AWSProvider, CloudProvider, get_provider, provider_for_cluster_type
from grl_config.training import GRLConfig

__all__ = [
    "GRLConfig",
    "ComputeConfig",
    "ComputeRoleConfig",
    "CloudProvider",
    "AWSProvider",
    "get_provider",
    "provider_for_cluster_type",
    "ROLLOUTS_RESOURCE",
    "TRAINING_RESOURCE",
]
