# Environment Manager

Environment-agnostic Rust gRPC server (`manager`) that fronts every environment during training. It implements `EnvironmentService` — VM lifecycle (create/evaluate/teardown), tool call dispatch, and scoring — and forwards execution into the Firecracker VM, where an environment-specific executor binary (e.g. [`swebench-lite/env`](../swebench-lite/env/)) implements the tools and computes the reward.

The manager contains no environment-specific code: environments vary only in the data they ship (VM images, manifest, in-VM executor, `tasks.jsonl`), never in the manager or the gRPC contract.

**Per-rollout lifecycle:** `CreateEnvironment → Execute* → Evaluate → Teardown`. The standard `submit` tool is handled locally by the manager; bash tools are forwarded to the in-VM executor (when wired).

**Task catalog.** At startup the manager loads `tasks.jsonl` from `GRL_TASKS_FILE` (synced by the DaemonSet initContainer from `GRL_BUNDLE_URI` into `{GRL_VM_CACHE_DIR}/active/`). Trainers call `ListTasks` for the task index (optionally filtered by split) instead of reading object storage. On `CreateEnvironment` the manager returns the task's opening prompt (`initial_messages_json`) and tool schemas (`tools_json`). The manager treats prompt/tools as opaque JSON.

**Admission control.** `CreateEnvironment` returns `RESOURCE_EXHAUSTED` when in-flight environments reach `GRL_MAX_CONCURRENT_ENVS` (default 32). Trainers retry with exponential backoff on a fresh Service connection so kube-proxy may reach another manager pod.

**Evaluate.** `Evaluate(env_id)` returns the task reward. The manager only relays it: the reward is computed inside the env executor, which runs the held-out test suite against the policy's edits (see [`swebench-lite/env/src/score.rs`](../swebench-lite/env/src/score.rs)). Callable once per env; after Evaluate only Teardown is valid.

The gRPC contract lives in [`environments/proto/grl/environment/v1/environment.proto`](../proto/grl/environment/v1/environment.proto) and is compiled at build time by `tonic-build` (see `build.rs`; uses a vendored `protoc`).

```bash
cargo run
```

In the cluster, the manager runs as a DaemonSet — one pod per environment node (see `infra/modules/resources/chart/templates/manager.yaml`), with the node-local VM image cache mounted read-only. Rollout workers dial the `grl-manager` ClusterIP Service to create an environment (which spreads environments across manager pods), then re-dial the owning pod directly using `CreateEnvironmentResponse.manager_addr` — advertised via `GRL_MANAGER_ADVERTISE_ADDR` from the downward-API pod IP — so every subsequent call for that environment reaches the pod that owns its VM, even across connection resets. Build the image from the repo root so the proto dir is in the build context:

```bash
docker build -f environments/manager/Dockerfile -t grl-manager .
```

Set `GRL_ENV_SERVER_ADDR` (default `0.0.0.0:50051` on the manager, `localhost:50051` in Python) to point clients at the manager.

**Environment activation (cluster).** Per-run bundle binding is done by the launcher (see [`launcher/PLAN.md`](../../launcher/PLAN.md)): patch `manager.bundleUri` / `manager.envId` in the Helm chart and rolling-restart the DaemonSet. Re-run `vms tasks` when rebuilding bundles so `tasks.jsonl` includes the standard `submit` tool schema. Relevant env vars:

| Variable | Purpose |
|----------|---------|
| `GRL_BUNDLE_URI` | S3 prefix synced by initContainer (`s3://…/datasets/…/dev`) |
| `GRL_TASKS_FILE` | Local path to catalog (`…/active/tasks.jsonl`) |
| `GRL_MANIFEST_FILE` | Local path to manifest (`…/active/manifest.json`) |
| `GRL_ENV_ID` | Environment name echoed in `ListTasksResponse.env_name` |
| `GRL_VM_CACHE_DIR` | Node-local cache root (`/var/lib/grl`) |
| `GRL_ACTIVE_DIR` | Subdir for active bundle (`active`) |
| `GRL_MANAGER_ADVERTISE_ADDR` | Pod IP:port returned as `manager_addr` |
| `GRL_MAX_CONCURRENT_ENVS` | Admission cap for in-flight rollout environments |
