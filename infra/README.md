# Infrastructure

This directory contains Terraform roots and reusable modules for the GRL Kubernetes environment.

## Terraform roots

- `aws/`: Full-stack root. Owns AWS-only VPC and EKS modules, then applies the shared cluster charts/resources into that new cluster.
- `byok/`: Bring-your-own-Kubernetes root. Applies the shared cluster charts/resources against an existing cluster using a supplied `kubeconfig_path`.

Both roots call the same reusable cluster modules under `modules/` so chart and CR definitions live in one place.

## Module Structure

- `aws/modules/vpc`: Creates the VPC, public/private subnets, internet gateway, NAT gateway, and route tables used by the EKS cluster.
- `aws/modules/cluster`: Creates the EKS control plane, shared IAM roles, and four node groups: `ray` (Ray head), `rollouts` (GPU vLLM inference), `training` (GPU), and `environments` (Firecracker environment workers).
  - Instance types, AMI types, disk size, and fixed node count for each node group are passed from the launcher via the role-keyed `node_groups` Terraform variable (derived from `compute` in the launcher config).
  - Each node group is labeled with `role=<role>`; use selectors such as `kubectl get nodes -l role=rollouts` instead of relying on provider-assigned node names.
  - The `rollouts` and `training` groups use the `AL2023_x86_64_NVIDIA` AMI (AL2-based GPU AMIs are deprecated and unavailable on EKS 1.33+) and carry the `nvidia.com/gpu` taint so only GPU workloads land on them.
  - The `environments` group must use bare-metal (`.metal`) instance types. EC2 only exposes `/dev/kvm` on bare metal, which the environment workers need to launch Firecracker microVMs from within their pods. These nodes are labeled `kvm=true` so Firecracker pods can target them with a `nodeSelector`. A variable validation rejects non-`.metal` instance types for this group.
- `modules/charts`: Installs cluster operators with Helm: OpenTelemetry Operator, NVIDIA GPU Operator, and KubeRay Operator.
- `modules/resources`: Installs Kubernetes custom resources after the operators are available. It currently creates a `RayCluster` and an `OpenTelemetryCollector` using a small local Helm chart.

## Installation Flow

The `aws` root applies the modules in dependency order:

1. `vpc` creates the network foundation.
2. `cluster` creates EKS using the VPC subnet outputs.
3. `charts` connects to EKS through the Helm provider and installs operator charts.
4. `resources` runs after `charts` and installs GRL resources.

The `byok` root skips VPC/EKS and applies only steps 3-4 against a kubeconfig file.

The `resources` module uses a local Helm chart instead of `kubernetes_manifest` so a first apply can plan before the CRDs exist. Helm applies the CR YAMLs only after Terraform has installed the operator charts.

## Usage

AWS full-stack:

```sh
cd infra/aws
terraform init
terraform plan
terraform apply
```

BYOK:

```sh
cd infra/byok
terraform init
terraform apply -var='kubeconfig_path=/path/to/kubeconfig'
```

After full-stack apply, use the generated output to configure local Kubernetes access:

```sh
terraform output kubeconfig_command
```
