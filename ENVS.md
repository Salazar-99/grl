# GRL Environments

GRL should make custom environments possible without requiring users to fork the
training stack or launcher. The clean public contract is to support two
environment integration modes:

1. **Managed Firecracker environments**: users provide a GRL-compatible
   environment bundle, and the GRL manager handles VM lifecycle, tool dispatch,
   scoring, and teardown.
2. **External gRPC environments**: users run their own service that implements
   the GRL environment gRPC API, and training connects to that service directly.

This split keeps the default SWE-bench-style VM path powerful while giving users
with their own simulator, sandbox, browser, game, hosted evaluator, or Kubernetes
service a lower-friction way to integrate.

## Public Packaging Model

The planned public surface should be:

- `grl`: the user-facing launcher package. It owns the CLI, config loading,
  packaged Terraform/Helm assets, image resolution, AWS/EKS orchestration, and
  run submission.
- `grl_proto`: the stable protobuf/gRPC contract used by training, launcher
  preflight checks, and external environment services.
- `grl_config`: the shared config schema used by the launcher and default
  training images.

For public users, a `grl` package version should correspond to compatible
default images, Helm chart templates, Terraform modules, config schema, and
proto contract. Version drift across those pieces is one of the easiest ways to
make a launcher painful to use.

## Environment Mode 1: Managed Firecracker Bundle

Use this mode when users want GRL to manage isolated task VMs with the default
Rust manager.

In this mode, users do not implement a custom gRPC server. They provide an
environment bundle and VM artifacts that comply with the manager's bundle
contract.

### Required Artifacts

The launcher points the manager at an S3 bundle prefix:

```yaml
environment:
  id: my-env
  bundle_uri: s3://my-bucket/datasets/my-env/dev
  split: dev
  server_addr: grl-manager.default.svc:50051

infra:
  vm_image_cache:
    bucket: my-bucket
```

The bundle must contain:

- `tasks.jsonl` at `environment.bundle_uri/tasks.jsonl`.
- VM image artifacts in the configured VM image cache bucket.
- A bootable base image that includes the in-VM executor.
- Per-task image artifacts or equivalent task disks referenced by `tasks.jsonl`.

Each `tasks.jsonl` row should include:

- `task_id`: stable task identifier.
- `split`: split label such as `train`, `dev`, or `test`.
- `messages`: OpenAI-style initial chat messages, serialized as JSON.
- `tools`: tool/function schemas, serialized as JSON.
- `base_image`: node-relative path to the bootable base ext4 image.
- `task_image`: node-relative path to the task ext4 image.

The manager treats `messages` and `tools` as opaque JSON and returns them from
`CreateEnvironment`. The trainer forwards them to the policy. The manager uses
`base_image` and `task_image` to boot Firecracker.

### In-VM Executor Contract

The base image must start an executor inside the VM. That executor is
environment-specific. It owns:

- Tool execution, such as `bash` or any custom tool schema exposed in `tools`.
- Persistent task state for the rollout.
- Reward computation during `Evaluate`.
- Any hidden answer keys, test suites, fixtures, or scoring logic.

The GRL manager should remain environment-agnostic. It should boot VMs, forward
tool calls, relay rewards, and tear down resources, but it should not know
SWE-bench-specific or user-environment-specific details.

### Tooling Needed For Public Use

This mode should not require users to reverse-engineer the SWE-bench-Lite
environment. Before treating custom managed environments as public API, add
commands like:

- `grl env init my-env`: scaffold a new environment bundle project.
- `grl env validate ./my-env`: validate `tasks.jsonl`, image paths, prompt JSON,
  tool JSON, and required artifacts.
- `grl env upload ./my-env s3://my-bucket/datasets/my-env/dev`: upload bundle
  metadata and artifacts.
- `grl env smoke-test`: run a minimal manager/client check against one task.

The important validation path is:

1. `ListTasks` returns the expected task catalog.
2. `CreateEnvironment` returns initial messages and tools for one task.
3. `Execute` can run at least one tool call.
4. `Evaluate` returns a reward and detail payload.
5. `Teardown` cleans up the environment.

## Environment Mode 2: External gRPC Service

Use this mode when users already have their own environment backend and do not
want GRL to manage Firecracker VMs.

In this mode, the user implements the GRL `EnvironmentService` gRPC API from
`grl_proto` and runs it wherever they want. The GRL launcher skips environment
activation and training connects directly to the supplied service address.

Example config:

```yaml
environment:
  id: custom-env
  split: train
  server_addr: my-env-service.default.svc:50051

launch:
  environment:
    activate: false
    verify: true
```

The custom service implements:

- `ListTasks`: return task IDs and splits, optionally filtered by split.
- `CreateEnvironment`: allocate or initialize one task environment and return
  the opening messages and tool schemas.
- `Execute`: run one policy-requested tool call against the environment.
- `Evaluate`: return the scalar reward, optional JSON details, and whether the
  failure was infrastructure-related.
- `Teardown`: clean up the allocated environment.

This is the lowest-friction extension point for users with existing systems:
browser environments, simulators, games, hosted code sandboxes, grading APIs, or
other Kubernetes services.

## Design Recommendations

- Treat the gRPC API as the primary environment contract.
- Treat the Firecracker bundle format as one managed implementation of that
  contract, not the only way to use GRL.
- Keep `grl_proto` dependency-light so external environment authors can install
  it without pulling in Ray, Kubernetes, Terraform, or training dependencies.
- Keep launcher-specific behavior in `grl`: AWS identity checks, S3 preflight,
  Helm upgrades, DaemonSet restarts, RayJob submission, and packaged infra.
- Document version compatibility between `grl`, `grl_proto`, `grl_config`,
  manager images, and training images.
- Provide validation and smoke-test commands before calling custom environments
  a stable public feature.

## Pitfalls To Avoid

- Do not imply that implementing the protobuf service is enough for managed
  Firecracker environments. Managed environments also require bundle layout, VM
  images, node-local cache compatibility, and an in-VM executor.
- Do not make users depend on the full training package just to implement an
  environment. Environment authors should only need `grl_proto`, plus their own
  runtime dependencies.
- Do not hide AWS cost or infrastructure assumptions. Launching EKS, GPU nodes,
  and bare-metal Firecracker nodes in a user's account should come with clear
  IAM, region, quota, cleanup, and cost guidance.
- Do not let default images, packaged infra, and config schema drift
  independently. A public `grl` release should be a tested compatibility set.

## Summary

The launcher-as-package plan is sound if the product surface is explicit:

- Users install `grl`.
- Users define config.
- Users launch into their own AWS account.
- Users choose default training images or custom training images.
- Users choose either a managed Firecracker environment bundle or an external
  gRPC environment service.

The most important next step is to turn the environment contract into a
first-class public spec, with validation tooling and a small example external
environment service.
