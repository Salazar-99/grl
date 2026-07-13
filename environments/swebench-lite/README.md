# SWEBench-Lite Environment for grl

This environment consists of a raw `data` directory, a `vms` module to generate firecracker vm images for each problem in the dataset, and an in-VM `env` executor that implements this environment's tools. VM lifecycle and tool call dispatch during training are handled by the shared, environment-agnostic [`manager`](../manager/) at the root of `environments/`.

## data/
This directory just contains the SWEBench-Lite dataset cloned [from HuggingFace here](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/main).
This is used by the `vms/` module to generate the Firecracker VM environments for use in training.

## vms/
Python tooling that builds Firecracker-ready squashfs disks for the SWE-bench-lite dev split.

**Base images** (`base-images/`) — one per repo+version environment. Ubuntu,
Python, and pip dependencies are baked in via Docker, then packed into a
read-only, zstd-compressed squashfs. Base images contain neither init logic nor
the environment executor.

**Task images** (`task-images/`) — one per dataset instance. Repo source at
`base_commit` and the private reward specification are packed into a read-only
squashfs. The external `grl-bootstrap` mounts it at `/run/grl/task`; the
environment package copies the task into its writable workspace.

**Bootstrap** (`bootstrap-images/`) — a required, content-addressed initramfs
containing the static `grl-bootstrap` PID 1. It assembles the writable root,
mounts task and environment packages, and supervises the environment entrypoint.
The bootstrap stays in the initramfs as PID 1; the entrypoint runs in a private
mount namespace chrooted to the assembled OverlayFS root.

**Environment package** (`environment-images/`) — a required squashfs
containing `/entrypoint` (`grl-env`). It owns SWE-bench workspace preparation,
tools, and scoring.

**Task dataset** (`tasks.jsonl`) — one line per instance with the index fields the trainer enumerates (`task_id`, `split`), the opening prompt (`messages`), tool schemas (`tools`), and node-relative VM image paths (`base_image`, `task_image`) for Firecracker boot.

### Prerequisites

- Docker (with buildx)
- [uv](https://docs.astral.sh/uv/)
- AWS CLI configured locally (`aws configure`) for uploads

### Usage

```bash
cd vms
uv sync

# Configure artifact uploads
export VMS_S3_BUCKET=my-bucket
export VMS_S3_REGION=us-west-2   # or set AWS_DEFAULT_REGION
```

The tooling requires an explicit operation; there is no base-image-only
fallback pipeline.

```bash
uv run vms generate          # write dockerfiles/
uv run vms build             # build base-images/*.squashfs (skips existing)
uv run vms build-tasks       # build task-images/*.squashfs (skips existing)
uv run vms build-bootstrap   # build the required grl-bootstrap initramfs
uv run vms build-environment # build the required grl-env package
uv run vms upload-bootstrap bootstrap-images/grl-bootstrap-<sha>.cpio.gz
uv run vms build-environment --upload --bundle-uri s3://<bucket>/<bundle>
uv run vms upload --jobs 4   # upload to s3://$VMS_S3_BUCKET/bases/ and .../tasks/
uv run vms tasks             # render tasks.jsonl (prompts + tools) for the trainer
uv run vms tasks --upload    # also upload to s3://$VMS_S3_BUCKET/datasets/swebench-lite/<split>/tasks.jsonl
uv run vms resolve <task_id>              # look up image paths from tasks.jsonl
uv run vms resolve <task_id> --tasks tasks.jsonl  # from a specific tasks.jsonl
```

Pass `--force` to rebuild or re-upload images that already exist. Use `--only <name>` with `build` or `build-tasks` to target a single image.

The Linux KVM publication gate builds a deterministic minimal fixture and runs
the production manager boot path in both direct and jailed modes. It verifies
cold boot, task/environment visibility, PTY support, Execute/Evaluate framing,
writable-clone isolation, snapshot creation, restore, and vsock reconnection.
For a local run, set `GRL_CONFORMANCE_KERNEL` to an uncompressed x86_64 kernel,
run `../conformance/build-fixture.sh`, then run the ignored
`manager/tests/kvm_conformance.rs` test.

Uploads land at `s3://$VMS_S3_BUCKET/bases/<env>.squashfs` and `s3://$VMS_S3_BUCKET/tasks/<instance_id>.squashfs`. Existing objects with matching size are skipped unless `--force` is set, so failed or interrupted uploads can be retried. Uploads run in parallel with `--jobs`, or `VMS_UPLOAD_JOBS`; the default is 4. Large files use S3 multipart upload, so objects appear in the bucket only after all parts complete.

## env/

Rust `env` executor binary that runs inside each Firecracker VM. It implements this environment's tools (a persistent `bash` shell) and computes the task reward (`src/score.rs`): on `Score` it applies the held-out test patch, runs the targeted tests from `/run/grl/task/task.json`, and returns reward 1.0 only if every `FAIL_TO_PASS` and `PASS_TO_PASS` test passes. It is packaged in `environment.squashfs`; it is never baked into a base image.

The environment-agnostic gRPC manager that handles VM lifecycle and tool call dispatch lives at [`environments/manager/`](../manager/) — it is shared across all environments and contains no swebench-lite-specific code.

```bash
# In-VM executor
cd env
cargo build
```

