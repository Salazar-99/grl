"""Shared infrastructure configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from grl_config.providers import CloudProvider, provider_for_cluster_type

ClusterType = Literal["EKS", "BYOK"]

# Ray custom resource names — must match KubeRay worker rayStartParams and actor
# .options(resources=...) at spawn time.
ROLLOUTS_RESOURCE = "rollouts"
TRAINING_RESOURCE = "training"

GPU_ROLES = frozenset({"rollouts", "training"})

COMPUTE_ROLES = ("ray", "rollouts", "training", "environments")


class ComputeRoleConfig(BaseModel):
    """Hardware sizing for one cluster role."""

    instance_type: str = Field(alias="instanceType")
    nodes: int
    disk_size: int = Field(alias="diskSize")
    ami_type: str | None = Field(default=None, alias="amiType")
    gpus_per_node: int | None = Field(default=None, alias="gpusPerNode")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_nodes(self) -> ComputeRoleConfig:
        if self.nodes < 0:
            raise ValueError("nodes must be greater than or equal to 0")
        return self

    def resolved_ami_type(self, role: str, cluster_type: ClusterType) -> str | None:
        if self.ami_type is not None:
            return self.ami_type
        provider = provider_for_cluster_type(cluster_type)
        if provider is None:
            return None
        return provider.default_ami_type(role)

    def resolved_gpus_per_node(self, cluster_type: ClusterType, *, role: str) -> int:
        if self.gpus_per_node is not None:
            return self.gpus_per_node
        if role not in GPU_ROLES:
            return 0
        provider = provider_for_cluster_type(cluster_type)
        if provider is None:
            raise ValueError(
                f"compute.{role}.gpus_per_node is required for {cluster_type} "
                f"(no provider SKU lookup configured)"
            )
        gpus = provider.lookup_gpus_per_instance(self.instance_type)
        if gpus is None:
            raise ValueError(
                f"unknown {provider.name} GPU instance type {self.instance_type!r} "
                f"for compute.{role}; set compute.{role}.gpus_per_node explicitly"
            )
        return gpus

    def terraform_node_group(self, role: str, cluster_type: ClusterType) -> dict[str, object]:
        provider = provider_for_cluster_type(cluster_type)
        if provider is not None:
            provider.validate_instance_type(role, self.instance_type)
        group: dict[str, object] = {
            "instance_types": [self.instance_type],
            "disk_size": self.disk_size,
            "node_count": self.nodes,
        }
        ami_type = self.resolved_ami_type(role, cluster_type)
        if ami_type is not None:
            group["ami_type"] = ami_type
        return group


class ComputeConfig(BaseModel):
    """Role-keyed compute sizing for GRL's cluster."""

    ray: ComputeRoleConfig = Field(
        default_factory=lambda: ComputeRoleConfig(
            instance_type="m5.4xlarge",
            nodes=2,
            disk_size=100,
        )
    )
    rollouts: ComputeRoleConfig = Field(
        default_factory=lambda: ComputeRoleConfig(
            instance_type="g4dn.xlarge",
            nodes=1,
            disk_size=200,
        )
    )
    training: ComputeRoleConfig = Field(
        default_factory=lambda: ComputeRoleConfig(
            instance_type="g4dn.xlarge",
            nodes=1,
            disk_size=200,
        )
    )
    environments: ComputeRoleConfig = Field(
        default_factory=lambda: ComputeRoleConfig(
            instance_type="c5.metal",
            nodes=1,
            disk_size=200,
        )
    )

    def validate_with_provider(self, provider: CloudProvider) -> None:
        for role in COMPUTE_ROLES:
            provider.validate_instance_type(role, getattr(self, role).instance_type)

    def node_groups_terraform_value(self, cluster_type: ClusterType) -> dict[str, dict[str, object]]:
        provider = provider_for_cluster_type(cluster_type)
        if provider is not None:
            self.validate_with_provider(provider)
        return {
            role: getattr(self, role).terraform_node_group(role, cluster_type)
            for role in COMPUTE_ROLES
        }

    def resolved_gpus_per_node(self, role: str, cluster_type: ClusterType) -> int:
        return getattr(self, role).resolved_gpus_per_node(cluster_type, role=role)

    def gpu_capacity(self, role: str, cluster_type: ClusterType) -> int:
        cfg = getattr(self, role)
        return cfg.nodes * cfg.resolved_gpus_per_node(cluster_type, role=role)
