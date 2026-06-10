# Infrastructure

This directory is a Terraform root module for the GRL AWS/EKS environment. The root module wires together smaller submodules under `modules/` and owns provider configuration, top-level variables, and outputs.

## Module Structure

- `modules/vpc`: Creates the VPC, public/private subnets, internet gateway, NAT gateway, and route tables used by the EKS cluster.
- `modules/cluster`: Creates the EKS control plane, shared IAM roles, and four node groups: `ray` (Ray head), `rollouts` (GPU vLLM inference), `training` (GPU), and `environment` (Firecracker environment workers).
  - Instance types, AMI types, disk size, and scaling (`desired`/`min`/`max`) for each node group are configurable from the root module via the `ray_nodes`, `rollouts_nodes`, `training_nodes`, and `environment_nodes` variables.
  - The `rollouts` and `training` groups use the `AL2023_x86_64_NVIDIA` AMI (AL2-based GPU AMIs are deprecated and unavailable on EKS 1.33+) and carry the `nvidia.com/gpu` taint so only GPU workloads land on them.
  - The `environment` group must use bare-metal (`.metal`) instance types. EC2 only exposes `/dev/kvm` on bare metal, which the environment workers need to launch Firecracker microVMs from within their pods. These nodes are labeled `kvm=true` so Firecracker pods can target them with a `nodeSelector`. A variable validation rejects non-`.metal` instance types for this group.
- `modules/charts`: Installs cluster operators with Helm: OpenTelemetry Operator, NVIDIA GPU Operator, and KubeRay Operator.
- `modules/resources`: Installs Kubernetes custom resources after the operators are available. It currently creates a `RayCluster` and an `OpenTelemetryCollector` using a small local Helm chart.

## Installation Flow

Terraform applies the modules in dependency order:

1. `vpc` creates the network foundation.
2. `cluster` creates EKS using the VPC subnet outputs.
3. `charts` connects to EKS through the Helm provider and installs the operator charts.
4. `resources` runs after `charts` via `depends_on = [module.charts]` and installs CRs that depend on operator-provided CRDs.

The `resources` module uses a local Helm chart instead of `kubernetes_manifest` so a first apply can plan before the CRDs exist. Helm applies the CR YAMLs only after Terraform has installed the operator charts.

## Usage

```sh
terraform init
terraform plan
terraform apply
```

After apply, use the generated output to configure local Kubernetes access:

```sh
terraform output kubeconfig_command
```
