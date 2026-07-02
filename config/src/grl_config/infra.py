"""Shared infrastructure configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class NodeGroupConfig(BaseModel):
    """EKS managed node group sizing and instance-type selection."""

    instance_types: list[str] = Field(alias="instanceTypes", min_length=1)
    ami_type: str = Field(alias="amiType")
    disk_size: int = Field(alias="diskSize")
    node_count: int = Field(alias="nodeCount")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_node_count(self) -> NodeGroupConfig:
        if self.node_count < 0:
            raise ValueError("node_count must be greater than or equal to 0")
        return self


class NodeGroupsConfig(BaseModel):
    """Role-keyed EKS node groups used by GRL's AWS cluster."""

    ray: NodeGroupConfig = Field(
        default_factory=lambda: NodeGroupConfig(
            instance_types=["m5.4xlarge"],
            ami_type="AL2023_x86_64_STANDARD",
            disk_size=100,
            node_count=2,
        )
    )
    rollouts: NodeGroupConfig = Field(
        default_factory=lambda: NodeGroupConfig(
            instance_types=["g4dn.xlarge"],
            ami_type="AL2023_x86_64_NVIDIA",
            disk_size=200,
            node_count=1,
        )
    )
    training: NodeGroupConfig = Field(
        default_factory=lambda: NodeGroupConfig(
            instance_types=["g4dn.xlarge"],
            ami_type="AL2023_x86_64_NVIDIA",
            disk_size=200,
            node_count=1,
        )
    )
    environments: NodeGroupConfig = Field(
        default_factory=lambda: NodeGroupConfig(
            instance_types=["c5.metal"],
            ami_type="AL2023_x86_64_STANDARD",
            disk_size=200,
            node_count=1,
        )
    )

    @model_validator(mode="after")
    def validate_environments_are_bare_metal(self) -> NodeGroupsConfig:
        if any(not instance.endswith(".metal") for instance in self.environments.instance_types):
            raise ValueError(
                "environments.instance_types must be bare-metal (.metal) instances"
            )
        return self

    def terraform_value(self) -> dict[str, dict[str, object]]:
        return self.model_dump(mode="json")
