"""AWS-specific compute provider (EKS / EC2)."""

from __future__ import annotations

_GPU_ROLES = frozenset({"rollouts", "training"})

# EC2 instance type (SKU) -> physical GPU count.
_EC2_GPUS_PER_INSTANCE: dict[str, int] = {
    # g4dn (NVIDIA T4)
    "g4dn.xlarge": 1,
    "g4dn.2xlarge": 1,
    "g4dn.4xlarge": 1,
    "g4dn.8xlarge": 1,
    "g4dn.16xlarge": 1,
    "g4dn.12xlarge": 4,
    "g4dn.metal": 8,
    # g5 (NVIDIA A10G)
    "g5.xlarge": 1,
    "g5.2xlarge": 1,
    "g5.4xlarge": 1,
    "g5.8xlarge": 1,
    "g5.16xlarge": 1,
    "g5.12xlarge": 4,
    "g5.24xlarge": 4,
    "g5.48xlarge": 8,
    # p3 (NVIDIA V100)
    "p3.2xlarge": 1,
    "p3.8xlarge": 4,
    "p3.16xlarge": 8,
    # p4d / p5 (NVIDIA A100 / H100)
    "p4d.24xlarge": 8,
    "p5.48xlarge": 8,
}

_AMI_GPU = "AL2023_x86_64_NVIDIA"
_AMI_STANDARD = "AL2023_x86_64_STANDARD"


class AWSProvider:
    """AWS EKS managed cluster: EC2 SKU tables and Firecracker bare-metal rules."""

    @property
    def name(self) -> str:
        return "aws"

    def lookup_gpus_per_instance(self, instance_type: str) -> int | None:
        return _EC2_GPUS_PER_INSTANCE.get(instance_type)

    def default_ami_type(self, role: str) -> str:
        return _AMI_GPU if role in _GPU_ROLES else _AMI_STANDARD

    def validate_instance_type(self, role: str, instance_type: str) -> None:
        if role == "environments" and not instance_type.endswith(".metal"):
            raise ValueError(
                "compute.environments.instance_type must be bare-metal (.metal) "
                "on AWS so /dev/kvm is available for Firecracker"
            )
