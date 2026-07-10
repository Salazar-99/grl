# grl

Distributed, async RLVR system for LLM post-training. grl lets you train an open-weights model with GRPO against any environment with a verifiable reward. The first environment is SWE-bench-Lite: each rollout is a multi-turn tool-use trajectory where the model edits and tests a real repository inside an isolated Firecracker microVM, and the reward comes from running the task's verification tests. Rollout generation, reward collection, and training run as pipelined async stages, so the GPUs generating trajectories and the GPUs updating weights are never blocked on each other.

**Components:**

- **`grl` launcher CLI** — one config-driven command that provisions infra, activates an environment bundle, and submits a training job
- **Ray (KubeRay on Kubernetes)** for distributed orchestration, each workload type runs as Ray actors on their own node group
- **vLLM** for rollout generation: async engine with continuous batching driving the agent loops
- **PyTorch / Hugging Face** for the GRPO trainer and model loading
- **Rust gRPC manager + Firecracker VMs** for stateful environments. We create one microVM per rollout booted from prebuilt images with an in-VM executor for running tool calls
- **Protobuf** to form a single API contract between rollout workers (Python) and the environment manager (Rust)
- **OTel Collector → ClickHouse → Grafana** as an external observability pipeline. We collect metrics from Ray, vLLM, DCGM (GPUs), and manually instrumented training code and forward them to a remote OTel Collector

## Project Structure

```
grl/
├── launcher/                  # `grl` CLI: Terraform/Helm/RayJob orchestration from one YAML config
├── config/                    # grl-config: shared Pydantic config models (used by launcher + training)
├── proto/                     # grl-proto: generated Python gRPC stubs + codegen script (checked in)
├── environments/
│   ├── proto/
│   │   └── grl/environment/v1/  # gRPC API contract (source of truth for both sides)
│   ├── manager/               # Environment-agnostic Rust gRPC `manager`: Firecracker VM lifecycle
│   │                          #   + tool call dispatch (one binary serves every environment)
│   └── swebench-lite/         # Everything needed to build Firecracker VM environments for this benchmark
│       ├── data/              # SWE-bench-Lite dataset
│       ├── vms/               # Python tooling: builds squashfs base/task images, renders
│       │                      #   tasks.jsonl, uploads bundles to S3
│       └── env/               # In-VM Rust `env` executor: implements the tools (persistent bash)
│                              #   and computes the task reward
├── training/                  # Core RL training loop on Ray
│   └── src/training/
│       ├── main.py            # Driver: task → rollout → batcher → trainer async loops
│       ├── rollouts.py        # RolloutWorker: vLLM async engine + multi-turn agent loop
│       ├── trainer.py         # TrainingWorker: GRPO updates, weight publishing
│       ├── environments.py    # gRPC client for EnvironmentService (one channel per env)
│       ├── checkpoints.py     # Background checkpoint uploads to S3
│       └── telemetry.py       # Manual OTel instrumentation (metrics, spans, trajectory logs)
├── infra/                     # Terraform roots + reusable modules (one root per provider)
│   ├── aws/                   # AWS root: VPC + EKS (aws/modules/) + shared charts/resources
│   ├── byok/                  # Bring-your-own-Kubernetes root: charts/resources against a kubeconfig
│   ├── modules/
│   │   ├── charts/            # Third-party operator installs (KubeRay, NVIDIA GPU Operator, OTel operator)
│   │   └── resources/chart/   # grl umbrella chart: RayCluster, manager DaemonSet, model/VM caches,
│   │                          #   OTel collector
│   └── observability/         # ClickHouse schema, external collector config, Grafana dashboard
└── docs/                      # Next.js documentation site
```

## Launcher

The `grl` CLI ([`launcher/`](launcher/)) drives everything from a single YAML config: training hyperparameters, hardware sizing (`compute`), environment bundle, images, and infra settings.

```bash
grl init config.yaml     # write a starter config
grl launch config.yaml   # provision + activate + submit
grl teardown config.yaml
```

`launch.deployment_type` selects how much of the stack to run — `CLUSTER` (provider infrastructure), `RESOURCES` (KubeRay cluster, manager DaemonSet, caches, collector), `ENVS` (per-run bundle sync), `TRAINING` (RayJob), or `FULL` (all four). Images resolve from a registry (`published`), explicit refs (`custom`), or a local build (`build_and_push`). Local state lives under `~/.grl`. See [`launcher/README.md`](launcher/README.md).

## Providers and deployment modes

grl runs on any Kubernetes cluster; provider-specific code is isolated to Terraform roots under [`infra/`](infra/) and instance-type resolvers under [`config/src/grl_config/providers/`](config/src/grl_config/providers/). Everything above that layer — the umbrella chart, the Ray cluster, and the training code — is provider-agnostic. Two modes are supported today, with more cloud providers planned:

- **AWS** — full-stack provisioning. The `CLUSTER` layer runs the Terraform root at [`infra/aws`](infra/aws) to create a VPC, an EKS cluster, and role-mapped node groups, then applies the shared operator charts and grl resources. GPUs per node are resolved automatically from `compute.<role>.instance_type` via the EC2 API.
- **BYOK (bring your own Kubernetes)** — point `launch.infra.kubeconfig` at an existing cluster. The [`infra/byok`](infra/byok) root installs the same operator charts and grl resources without creating any cloud infrastructure. The cluster must already provide nodes with the expected `role` labels, GPU support and taints, and KVM access on environment nodes; `gpus_per_node` is set explicitly in config.

Both roots share the same modules under `infra/modules/`, so chart and resource definitions live in one place. Adding a provider means adding a Terraform root that produces the same role-labeled node groups, plus an instance-type resolver.

## Infra

The workload runs on a single Ray cluster (KubeRay) over a Kubernetes cluster. Each kind of work gets its own node group, a matching Ray worker group, and a dedicated container image — so a Ray actor is scheduled onto the right hardware and runs with only the dependencies it needs.

The binding is a **custom Ray resource**: each worker group advertises a uniquely-named resource (e.g. `{"rollouts": N}`), and each actor requests that same name via `@ray.remote(resources=...)`. Ray's scheduler can then only place the actor on a node from the matching group.

| Node group | `role` label | Ray worker group | Custom resource | Actor | Image |
|----------------|--------------|------------------|-----------------|-------|-------|
| `ray` | `ray` | head group | — | (Ray head + training driver) | `head` |
| `rollouts` | `rollouts` | `rollouts` | `{"rollouts": N}` | `RolloutWorker` (vLLM inference) | `rollouts` |
| `training` | `training` | `training` | `{"training": N}` | `TrainingWorker` (GRPO) | `training` |
| `environments` | `environments` | — (not in the Ray cluster) | — | `manager` DaemonSet (gRPC → Firecracker) | `grl-manager` |

- **GPU groups** (`rollouts`, `training`) carry the `nvidia.com/gpu` taint, so only GPU pods land on them. `N` is the GPUs advertised per node — resolved by the launcher from `compute.<role>.instance_type` via the provider resolvers in [`config/src/grl_config/providers/`](config/src/grl_config/providers/) (or an explicit `gpus_per_node` on BYOK). Multi-GPU training is not yet implemented (see ARCHITECTURE.md).
- **`environments` group** needs bare-metal nodes because Firecracker requires `/dev/kvm`; nodes are labeled `kvm=true` (on AWS, a variable validation enforces `.metal` instance types). These nodes don't run Ray: the Rust `manager` runs as a plain Kubernetes DaemonSet (one pod per node) and rollout workers reach it directly over gRPC through the `grl-manager` Service.
- **Sizing** (instance type, disk, node count) for each role is set in the top-level `compute` section of the launcher config; the launcher derives provider node groups, Ray worker replicas, and actor counts from it. On BYOK, `nodes` sizes Ray worker pod replicas instead of provisioning nodes.

The mapping is defined in three places that must agree on the names: the provider node groups (e.g. [`infra/aws/modules/cluster`](infra/aws/modules/cluster)), the RayCluster worker groups ([`infra/modules/resources/chart/templates/raycluster.yaml`](infra/modules/resources/chart/templates/raycluster.yaml)), and the actor decorators in [`training/src/training/`](training/src/training/). Per-role images are built from [`training/Dockerfile`](training/Dockerfile) (one build target per role, each installing only that role's `uv` extra).

The umbrella chart also deploys node-local caches: a model cache on GPU nodes and a VM image cache on environment nodes (base/task images plus the active bundle's `tasks.jsonl`, synced from S3).

## Environments

An environment is data, not code: prebuilt Firecracker VM images, a `tasks.jsonl` catalog, and an in-VM executor binary. The shared Rust [`manager`](environments/manager/) handles VM lifecycle and tool dispatch for every environment and contains no environment-specific logic.

**Per-rollout lifecycle:** `CreateEnvironment → Execute* → Evaluate → Teardown`. On `CreateEnvironment` the manager boots a microVM from the task's images, waits for the in-VM executor on vsock, and returns the task's opening prompt and tool schemas. Tool calls are forwarded to the executor; `Evaluate` relays the reward the executor computes in-VM. Admission is capped per pod by `GRL_MAX_CONCURRENT_ENVS` (default 32), so total environment capacity ≈ environment nodes × that cap.

**SWE-bench-Lite** ([`environments/swebench-lite/`](environments/swebench-lite/)) is the first environment:

- **Base images** — one read-only squashfs per repo+version, with Ubuntu, Python, and dependencies baked in. Each boots with a per-VM writable overlay so one image safely serves all the concurrent VMs GRPO fans out per task.
- **Task images** — one small squashfs per dataset instance with the repo at `base_commit`, mounted read-only and copied into the writable `/testbed`.
- **`tasks.jsonl`** — one line per instance: `task_id`, split, opening prompt, tool schemas, and VM image paths. It carries no answer keys; the reward spec (held-out tests, test patch) is baked into each task image at `/grl/task.json` where only the in-VM scorer reads it.
- **`env` executor** — implements the tools (a persistent bash shell) and scoring: it applies the held-out test patch, runs the targeted tests, and returns reward 1.0 only if every `FAIL_TO_PASS` and `PASS_TO_PASS` test passes.

The `vms` tooling builds all of this and uploads it to S3 as a bundle; the launcher's `ENVS` layer syncs the bundle onto environment nodes and rolling-restarts the manager DaemonSet to activate it.

### Implementing a new environment

The training stack only speaks the `EnvironmentService` gRPC API, so there are two ways to bring a new environment (see [`ENVS.md`](ENVS.md) for the longer discussion):

**Managed Firecracker bundle** — no manager or trainer code changes; you ship data that satisfies the bundle contract:

- **`tasks.jsonl`** — one row per task with `task_id`, `split`, `messages` (opening prompt, OpenAI-style JSON), `tools` (tool schemas, must include the standard `submit` tool), and node-relative `base_image` / `task_image` paths. The manager treats `messages` and `tools` as opaque JSON and just returns them from `CreateEnvironment`.
- **A bootable base image** — a squashfs that boots under Firecracker and starts your in-VM executor listening on vsock port 5005. The executor speaks the framed-protobuf relay protocol (the `Execute`/`Evaluate` messages from the shared proto): it implements whatever tools your `tools` schemas declare and computes the reward on `Evaluate`. Anything the policy must not see — answer keys, held-out tests, scoring logic — lives inside the image (the `submit` tool itself is handled by the manager).
- **Per-task images** — small read-only disks with per-task state, referenced from `tasks.jsonl` and mounted at boot.

Upload the bundle to S3, set `environment.bundle_uri` in the launch config, and the `ENVS` layer activates it. VM lifecycle, tool dispatch, admission control, and teardown all come for free from the manager.

**External gRPC service** — if you already have a sandbox, simulator, or hosted evaluator, skip VMs entirely: implement the five RPCs (`ListTasks`, `CreateEnvironment`, `Execute`, `Evaluate`, `Teardown`) in your own service and point `environment.server_addr` at it. Only the `grl-proto` package is needed to implement the contract.

## Training

The driver ([`training/src/training/main.py`](training/src/training/main.py)) runs on the Ray head via a RayJob and wires four concurrent async loops over bounded queues:

```
task_loop ──► rollout_loop ──► batcher_loop ──► trainer_loop
 (shuffle       (dispatch to      (assemble         (GRPO update,
  task ids       RolloutWorkers,   GRPO groups       publish weights)
  per epoch)     cap in-flight)    into batches)
```

- **`RolloutWorker`** (one per rollouts GPU ÷ `tensor_parallel_size`) owns a vLLM `AsyncLLM` engine and runs many trajectories concurrently. Each trajectory is a multi-turn agent loop — generate, parse tool calls, execute them in the environment over gRPC, append observations — ending when the model calls `submit` or hits a turn/timeout limit.
- **`batcher_loop`** assembles rollouts into GRPO groups (all rollouts for one task). Groups that time out are padded with `infra_error` placeholders, and partially filled batches are flushed when the oldest group exceeds `max_policy_staleness` policy versions — keeping the pipeline async without training on stale data.
- **`TrainingWorker`** (exactly one, single GPU) computes GRPO advantages per group (skipping groups with too few valid rollouts), updates the policy, and publishes new weights to rollout workers through the Ray object store. Checkpoints upload to S3 in the background at a configurable step interval.

Training hyperparameters, worker counts, and timeouts all come from the shared `GRLConfig` models in [`config/`](config/); the launcher passes the training section of the launch config through to the RayJob via a ConfigMap.

## Proto

The environment manager (Rust) and training workers (Python) communicate over gRPC. The API contract is defined once in protobuf and compiled into language-specific stubs on each side.

**Source of truth:** [`environments/proto/grl/environment/v1/environment.proto`](environments/proto/grl/environment/v1/environment.proto)

That file defines `EnvironmentService` — the RPCs used to list tasks, create environments, execute tools inside a VM, evaluate, and tear down. Both services depend on this file, not on each other's generated code.

**How it fits together:**

```
RolloutWorker (GPU, Python)  ──gRPC──►  manager (Rust, DaemonSet)  ──►  Firecracker VM + env executor
```

During rollouts, `RolloutWorker` creates one environment per trajectory (`EnvironmentSession` in `training/environments.py`) and dispatches tool calls to it over gRPC. Environments are created through the `grl-manager` ClusterIP Service (spreading them across manager pods); each session then re-dials the owning pod directly via the `manager_addr` returned by `CreateEnvironment`, so every call for an environment reaches the pod that owns its VM.

**Codegen:**

| Language | Tool | Output |
|----------|------|--------|
| Rust | `tonic-build` in `environments/manager/build.rs` | compiled into the manager crate at build time |
| Python | `uv run generate-proto` in `proto/` | `proto/src/grl_proto/` (checked in) |

Rust uses a vendored `protoc` binary, so no system install is required. Python codegen uses the `generate-proto` console script in the `grl-proto` package:

```bash
cd proto
uv sync --group dev
uv run generate-proto
```

**Configuration:** set `GRL_ENV_SERVER_ADDR` to point the Python client at the manager (default `localhost:50051` locally; in-cluster it's set to the `grl-manager` Service).

**When you change the proto:**

1. Edit `environments/proto/grl/environment/v1/environment.proto`.
2. Regenerate Python stubs: `uv run generate-proto` (from `proto/`).
3. Rebuild the Rust manager: `cargo build` (from `environments/manager/`; runs `tonic-build` automatically).
4. Update the Rust service impl in `environments/manager/src/environment.rs` and the Python client in `training/src/training/environments.py`.

For breaking API changes, bump the package version (e.g. `grl.environment.v2`) rather than modifying `v1` in place.

## Observability

Metrics, traces, and trajectory logs flow through two collectors: an in-cluster OTel collector (deployed by the umbrella chart) and an external one that owns the ClickHouse export.

**In-cluster collector** receives from two directions:

- **Push (OTLP):** every training process — driver, `TrainingWorker`, each `RolloutWorker` — and the Rust manager are manually instrumented ([`training/telemetry.py`](training/src/training/telemetry.py), [`manager/src/telemetry.rs`](environments/manager/src/telemetry.rs)). Metrics like queue depths, group assembly time, rewards, and step timings, plus per-trajectory logs.
- **Scrape (Prometheus):** Ray node metrics from every Ray pod, vLLM engine metrics from a per-`RolloutWorker` Prometheus server, and GPU metrics from the GPU Operator's dcgm-exporter.

Everything is forwarded upstream (with basic auth) to the external collector, which writes to ClickHouse using the schema in [`infra/observability/schema.sql`](infra/observability/schema.sql) — landing tables plus materialized views keyed on the `run.id` resource attribute, which the driver generates per run and threads through every actor. [`infra/observability/grafana/`](infra/observability/grafana/) holds the generated Grafana dashboard.
