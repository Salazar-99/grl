"""Cloud provider abstractions for compute sizing and validation."""

from __future__ import annotations

from typing import Protocol

from grl_config.providers.aws import AWSProvider


class CloudProvider(Protocol):
    """Provider-specific SKU lookup, AMI defaults, and instance validation."""

    @property
    def name(self) -> str: ...

    def lookup_gpus_per_instance(self, instance_type: str) -> int | None: ...

    def default_ami_type(self, role: str) -> str: ...

    def validate_instance_type(self, role: str, instance_type: str) -> None: ...


_REGISTRY: dict[str, CloudProvider] = {
    "aws": AWSProvider(),
}


def get_provider(name: str) -> CloudProvider | None:
    return _REGISTRY.get(name)


def provider_for_cluster_type(cluster_type: str) -> CloudProvider | None:
    """Return the managed-cloud provider for a cluster type, if any."""
    if cluster_type == "EKS":
        return get_provider("aws")
    return None
