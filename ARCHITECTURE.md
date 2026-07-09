# GRL Architecture

How the system is laid out today, how workloads are scheduled, and how configuration propagates through the stack.

## Overview

GRL trains an LLM with GRPO on real software-engineering tasks. Rollout generation (vLLM), environment execution (Firecracker VMs), and policy updates (PyTorch) run as concurrent async stages on separate hardware.

```
┌─────────────────────────────────────────────────────────────────┐
│  Ray head (driver)                                              │
│  task_loop → rollout_loop → batcher_loop → trainer_loop         │
└────────┬──────────────────────┬─────────────────────────────────┘
         │ Ray actors          │ Ray actors
         ▼                     ▼
┌─────────────────┐   ┌─────────────────┐
│ RolloutWorker   │   │ TrainingWorker  │
│ (rollouts GPU)  │   │ (training GPU)  │
│ vLLM + agent    │   │ GRPO update     │
└────────┬────────┘   └─────────────────┘
         │ gRPC
         ▼
┌─────────────────┐
│ manager         │
│ (env nodes)     │──► Firecracker microVM per trajectory
└─────────────────┘
```

**Launcher** (`launcher/`) provisions infra, syncs environment bundles, and submits a KubeRay `RayJob`. **Training** (`training/`) is the Ray driver + actor code. **Infra** (`infra/`) is Terraform + Helm for EKS, node groups, and the resources chart.

## Deployment layers

`launch.deployment_type` in config runs one of four stacks (each requires the layer below):

| Layer | What it deploys |
|-------|-----------------|
| `CLUSTER` | VPC + EKS node groups |
| `RESOURCES` | KubeRay cluster, OTel collector, manager DaemonSet, model/VM caches |
| `ENVS` | Per-run bundle sync onto environment nodes |
| `TRAINING` | RayJob that runs `training.main` |
| `FULL` | All four in order |

See `launcher/example-config.yaml` and `launcher/README.md` for the full config surface.

## Compute and node groups

Hardware sizing lives in a top-level `compute` section. Each role has `instance_type`, `nodes`, and `disk_size`; the launcher resolves everything else and propagates it to Terraform, Helm, and the training payload.

| Role | Hardware | Workloads |
|------|----------|-----------|
| `ray` | CPU (e.g. `m5.large`) | Ray head pod; training driver via RayJob |
| `rollouts` | GPU (e.g. `g5.xlarge`) | KubeRay rollouts worker pods → `RolloutWorker` actors |
| `training` | GPU | KubeRay training worker pods → `TrainingWorker` actor |
| `environments` | Bare metal (`.metal`, needs `/dev/kvm`) | `manager` DaemonSet (one pod per node); not part of Ray |

On EKS, `compute.<role>.nodes` provisions that many nodes in the matching node group (labeled `role: <role>`; GPU groups tainted `nvidia.com/gpu=true:NoSchedule`). On BYOK, `nodes` means KubeRay worker pod replicas (no nodes are provisioned).

The launcher resolves `gpus_per_node` from `instance_type` via cloud provider implementations in `config/src/grl_config/providers/` (AWS EC2 for EKS), or from an explicit `gpus_per_node` field (required on BYOK and unknown SKUs). It then derives:

- EKS `node_groups` for Terraform
- KubeRay worker `replicas` and `gpusPerNode` for Helm
- `workers.num_rollout_workers` for the RayJob (`nodes × gpus_per_node ÷ rollout.tensor_parallel_size`, overridable)

Environment nodes also run the VM image cache and per-run bundle-sync DaemonSets. GPU nodes run model-cache; GPU metrics come from the NVIDIA GPU Operator's dcgm-exporter.

## Ray scheduling

Work is pinned to the right hardware via **custom Ray resources** (`rollouts`, `training`). Names are defined once in `grl_config.infra` and used by the Helm chart and actor spawn options in `training.main`.

| Worker group | Ray advertises | Actor requests |
|--------------|----------------|----------------|
| `rollouts` | `num-gpus: N`, `resources: {"rollouts": N}` | `.options(num_gpus=tp, resources={"rollouts": tp})` |
| `training` | `num-gpus: N`, `resources: {"training": N}` | `.options(num_gpus=1, resources={"training": 1})` |

`N` (`gpusPerNode`) is the resolved GPUs per node from `compute`. Rollout actors request `tp = rollout.tensor_parallel_size` GPUs each for vLLM tensor parallelism (single-node only).

KubeRay worker pods also request `nvidia.com/gpu: N` so the kubelet reserves physical GPUs.

## Training actors

When the RayJob starts, `training.main` creates:

- `workers.num_rollout_workers` instances of `RolloutWorker` (derived from `compute` unless overridden)
- Exactly **one** `TrainingWorker` (hardcoded)

Each `RolloutWorker` owns one vLLM `AsyncLLM` engine (optionally sharded across `rollout.tensor_parallel_size` GPUs) and runs many trajectories concurrently (`rollout.max_concurrent_trajectories`). The driver caps total outstanding rollout RPCs with `workers.max_in_flight_rollouts`.

`grpo.num_rollouts` is rollouts **per GRPO group**, not a worker count.

## Environment path

Rollout workers reach the manager at `environment.server_addr` (default `grl-manager.default.svc:50051`). The rollouts worker pod sets `GRL_ENV_SERVER_ADDR` to that Service.

- Manager runs as a DaemonSet on `role: environments` nodes.
- Each trajectory gets one gRPC channel → one manager pod (kube-proxy pins the TCP connection).
- Per-pod admission is capped by `manager.max_concurrent_envs` (default 32).
- Total environment capacity ≈ `compute.environments.nodes × max_concurrent_envs`.

## Resource exposure

| Layer | Mechanism |
|-------|-----------|
| K8s placement | `nodeSelector: role: …`, GPU taints/tolerations |
| Physical GPUs | `nvidia.com/gpu` requests on Ray worker pods |
| Ray capacity | `num-gpus` + custom resources in `rayStartParams` |
| Actor placement | `.options(num_gpus=…, resources={…})` at spawn time |
| vLLM metrics | `RolloutWorker` starts a Prometheus server on `rollout.vllm_metrics_port` |
| GPU metrics | NVIDIA GPU Operator dcgm-exporter (scraped by OTel collector) |
| Manager / training metrics | OTel SDK → in-cluster collector → upstream endpoint |

## Sizing correlation (example-config defaults)

With `compute.rollouts.nodes: 1`, `instance_type: g5.xlarge` (1 GPU), `tensor_parallel_size: 1`, and default derivation:

| Resource | Capacity |
|----------|----------|
| RolloutWorker actors | 1 (`nodes × gpusPerNode ÷ tensor_parallel_size`) |
| TrainingWorker actors | 1 |
| Concurrent trajectories per rollout actor | up to `max_concurrent_trajectories` (32) |
| Concurrent environments cluster-wide | up to `compute.environments.nodes × max_concurrent_envs` |

The launcher prints a capacity summary at preflight and errors when rollout workers exceed GPU capacity.

## Gaps

### 1. Environment capacity is separate from Ray

Environment throughput is bounded by manager DaemonSet pods and `max_concurrent_envs`, not by rollout node count. High rollout concurrency can exhaust environment admission before GPU capacity; preflight warns when this may happen.

### 2. README vs code on training GPUs

`README.md` states the training group needs ≥2 GPUs (policy + reference model). `TrainingWorker` currently requests 1 GPU and loads a single model on `cuda:0`. Multi-GPU training is not implemented.

### 3. Spare ray head capacity

Default `compute.ray.nodes: 2` provisions two CPU nodes but only schedules one head pod. The second node is spare unless used for other CPU workloads.

---

For component-level detail see `README.md`. For launcher usage see `launcher/README.md`. For a worked example config see `launcher/example-config.yaml`.
