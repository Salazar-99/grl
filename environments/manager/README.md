# Environment Manager

Environment-agnostic Rust gRPC server (`manager`) that fronts every environment during training. It implements `EnvironmentService` — VM lifecycle (create/reset/close), tool call dispatch, and scoring — and forwards execution into the Firecracker VM, where an environment-specific executor binary (e.g. [`swebench-lite/env`](../swebench-lite/env/)) implements the tools and computes the reward.

The manager contains no environment-specific code: environments vary only in the data they ship (VM images, manifest, in-VM executor, `tasks.jsonl`), never in the manager or the gRPC contract.

**Task catalog.** On `CreateEnvironment` the manager returns the task's opening prompt (`initial_messages_json`) and tool schemas (`tools_json`) so the trainer stays task-agnostic. It loads these from the environment's `tasks.jsonl` (rendered by `vms`), pointed at by `GRL_TASKS_FILE` (a local path; infra mounts the object that the trainer's `GRL_TASKS_S3_URI` points at). The manager treats the prompt/tools as opaque JSON — the environment owns their content.

**Scoring.** `Score(env_id)` returns the task reward. The manager only relays it: the reward is computed inside the env executor, which runs the held-out test suite against the policy's edits (see [`swebench-lite/env/src/score.rs`](../swebench-lite/env/src/score.rs)).

The gRPC contract lives in [`environments/proto/grl/environment/v1/environment.proto`](../proto/grl/environment/v1/environment.proto) and is compiled at build time by `tonic-build` (see `build.rs`; uses a vendored `protoc`).

```bash
cargo run
```

In the cluster, the manager runs as a DaemonSet — one pod per environment node (see `infra/modules/resources/chart/templates/manager.yaml`), with the node-local VM image cache mounted read-only. Rollout workers dial the `grl-manager` ClusterIP Service to create an environment (which spreads environments across manager pods), then re-dial the owning pod directly using `CreateEnvironmentResponse.manager_addr` — advertised via `GRL_MANAGER_ADVERTISE_ADDR` from the downward-API pod IP — so every subsequent call for that environment reaches the pod that owns its VM, even across connection resets. Build the image from the repo root so the proto dir is in the build context:

```bash
docker build -f environments/manager/Dockerfile -t grl-manager .
```

Set `GRL_ENV_SERVER_ADDR` (default `0.0.0.0:50051` on the manager, `localhost:50051` in Python) to point clients at the manager.
