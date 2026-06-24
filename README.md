# grl

Distributed, async RLVR system for LLM post-training. It trains an open-weights policy model with GRPO against agentic software-engineering tasks from SWE-bench-Lite. Each rollout is a multi-turn tool-use trajectory where the model edits and tests a real repository inside an isolated Firecracker microVM. Rewards come from running the task's verification tests. Rollout generation, reward collection, and training run as pipelined async stages, so the GPUs generating trajectories and the GPUs updating weights are never blocked on each other.

**Components:**

- **Ray (KubeRay on EKS)** for distributed orchestration, each workload type runs as Ray actors on their own node group
- **vLLM** for rollout generation: async engine with continuous batching driving the agent loops
- **PyTorch / Hugging Face** for the GRPO trainer and model loading
- **Rust gRPC manager + Firecracker VMs** for stateful environments. We create one microVM per rollout booted from prebuilt images with an in-VM executor for running tool calls
- **Protobuf** to form a single API contract between rollout workers (Python) and the environment manager (Rust)
- **OTel Collector → ClickHouse → Grafana** as an external observability pipeline. We collect metrics from Ray, vLLM, DCGM (GPUs), and manually instrumented training code and forward them to a remote OTel Collector

## Project Structure

```
grl/
├── environments/
│   ├── proto/
│   │   └── grl/environment/v1/  # gRPC API contract (source of truth for both sides)
│   ├── manager/               # Shared Rust gRPC `manager` to handle the VM lifecycle + tool call dispatch
│   │                          #   (environment-agnostic; one binary serves every environment)
│   └── swebench-lite/         # Everything needed to build Firecracker VM environments for this benchmark
│       ├── data/              # SWE-bench-Lite dataset
│       ├── vms/               # Python tooling: builds Firecracker ext4 base/task images,
│       │                      #   writes manifest.json, uploads them to S3
│       └── env/               # In-VM `env` executor binary that implements this environment's tools
│                              #   and runs them inside the VM in a persistent TTY
|
├── training/                  # Core RL training loop on Ray
│   └── src/training/
│       ├── main.py            # Main async pipeline: submitter → batcher → trainer loops
│       ├── rollouts.py        # RolloutWorker: vLLM async engine + multi-turn agent loop
│       ├── trainer.py         # TrainingWorker: GRPO updates, weight publishing
│       ├── environments.py    # EnvironmentSession: gRPC client for EnvironmentService (one channel per env)
│       └── proto/             # generated Python stubs for gRPC client
|
└── infra/                     # Terraform module with submodules to provision a VPC, EKS cluster, and Helm charts with grl specific config
    └── modules/
        ├── vpc/               # networking
        ├── cluster/           # EKS cluster + node groups mapped to system components (ray head/rollouts/training/environment)
        ├── charts/            # third-party helm installs (KubeRay, NVIDIA GPU operator, OTel operator)
        └── resources/chart/   # grl resources and config in a single umbrella chart (RayCluster, OTel collector, dcgm-exporter)
```

## Infra

The workload runs on a single Ray cluster (KubeRay) over an EKS cluster. Each kind of work gets its own EKS node group, a matching Ray worker group, and a dedicated container image — so a Ray actor is scheduled onto the right hardware and runs with only the dependencies it needs.

The binding is a **custom Ray resource**: each worker group advertises a uniquely-named resource (e.g. `{"rollouts": N}`), and each actor requests that same name via `@ray.remote(resources=...)`. Ray's scheduler can then only place the actor on a node from the matching group.

| EKS node group | `role` label | Ray worker group | Custom resource | Actor | Image |
|----------------|--------------|------------------|-----------------|-------|-------|
| `ray` | `ray` | head group | — | (Ray head) | `head` |
| `rollouts` | `rollouts` | `rollouts` | `{"rollouts": N}` | `RolloutWorker` (vLLM inference) | `rollouts` |
| `training` | `training` | `training` | `{"training": N}` | `TrainingWorker` (GRPO) | `training` |
| `environment` | `environment` | — (not in the Ray cluster) | — | `manager` DaemonSet (gRPC → Firecracker) | `grl-manager` |

- **GPU groups** (`rollouts`, `training`) carry the `nvidia.com/gpu` taint, so only GPU pods land on them. `N` is the GPUs advertised per node — derived automatically from the chosen instance type via a lookup map in [`infra/locals.tf`](infra/locals.tf), so picking an 8-GPU instance advertises 8 without editing the chart. The `training` group needs ≥2 GPUs (policy on `cuda:0`, reference model on `cuda:1`).
- **`environment` group** uses bare-metal (`.metal`) instances because Firecracker needs `/dev/kvm`; nodes are labeled `kvm=true` and a variable validation enforces `.metal`. These nodes don't run Ray: the Rust `manager` runs as a plain Kubernetes DaemonSet (one pod per node) and rollout workers reach it directly over gRPC through the `grl-manager` Service.
- **Sizing** (instance types, AMI, disk, scaling) for each group is set from the root module via the `ray_nodes`, `rollouts_nodes`, `training_nodes`, and `environment_nodes` variables ([`infra/variables.tf`](infra/variables.tf)).

The mapping is defined in three places that must agree on the names: the EKS node groups ([`infra/modules/cluster`](infra/modules/cluster)), the RayCluster worker groups ([`infra/modules/resources/chart/templates/raycluster.yaml`](infra/modules/resources/chart/templates/raycluster.yaml)), and the actor decorators in [`training/src/training/`](training/src/training/). Per-role images are built from [`training/Dockerfile`](training/Dockerfile) (one build target per role, each installing only that role's `uv` extra).

## Environments

## Training

## Proto

The environment manager (Rust) and training workers (Python) communicate over gRPC. The API contract is defined once in protobuf and compiled into language-specific stubs on each side.

**Source of truth:** [`environments/proto/grl/environment/v1/environment.proto`](environments/proto/grl/environment/v1/environment.proto)

That file defines `EnvironmentService` — the RPCs used to create environments, execute tools inside a VM, reset, and tear down. Both services depend on this file, not on each other's generated code.

**How it fits together:**

```
RolloutWorker (GPU, Python)  ──gRPC──►  manager (Rust, DaemonSet)  ──►  Firecracker VM + env executor
```

During rollouts, `RolloutWorker` creates one environment per trajectory (`EnvironmentSession` in `training/environments.py`) and dispatches tool calls to it over gRPC. Each session holds its own channel, so all calls for an environment reach the manager pod that owns its VM.

**Codegen:**

| Language | Tool | Output |
|----------|------|--------|
| Rust | `tonic-build` in `environments/manager/build.rs` | compiled into the manager crate at build time |
| Python | `uv run generate-proto` in `training/` | `training/src/training/proto/` (checked in) |

Rust uses a vendored `protoc` binary, so no system install is required. Python codegen uses the `generate-proto` console script in the training package:

```bash
cd training
uv sync --group dev
uv run generate-proto
```

**Configuration:** set `GRL_ENV_SERVER_ADDR` to point the Python client at the manager (default `localhost:50051`; the manager listens on `0.0.0.0:50051`).

**When you change the proto:**

1. Edit `environments/proto/grl/environment/v1/environment.proto`.
2. Regenerate Python stubs: `uv run generate-proto` (from `training/`).
3. Rebuild the Rust manager: `cargo build` (from `environments/manager/`; runs `tonic-build` automatically).
4. Update the Rust service impl in `environments/manager/src/environment.rs` and the Python client in `training/src/training/environments.py`.

For breaking API changes, bump the package version (e.g. `grl.environment.v2`) rather than modifying `v1` in place.

**What to do next:**

- Implement `EnvironmentService` in Rust — wire `CreateEnvironment`, `Execute`, `Reset`, and `Close` to Firecracker VM lifecycle and the in-VM `env` executor binary.
- Add integration tests that start the manager and exercise the RPCs from Python.
- Once the API stabilizes, consider adding [Buf](https://buf.build) at the repo root for linting and breaking-change detection.

## Observability
