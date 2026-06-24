# SWEBench-Lite Environment for grl

This environment consists of a raw `data` directory, a `vms` module to generate firecracker vm images for each problem in the dataset, and an in-VM `env` executor that implements this environment's tools. VM lifecycle and tool call dispatch during training are handled by the shared, environment-agnostic [`manager`](../manager/) at the root of `environments/`.

## data/
This directory just contains the SWEBench-Lite dataset cloned [from HuggingFace here](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/main).
This is used by the `vms/` module to generate the Firecracker VM environments for use in training.

## vms/
Python tooling that builds Firecracker-ready ext4 disks for the SWE-bench-lite dev split.

**Base images** (`base-images/`) — one per repo+version environment. Ubuntu, Python, and pip dependencies are baked in via Docker, then exported to an ext4 disk sized to fit the content plus 512 MB of runtime headroom (typically ~1–1.8 GB). Each base image is boot-ready: `/init` mounts the task disk at `/testbed` and starts `grl-env` on vsock port 5005.

**Task images** (`task-images/`) — one per dataset instance. Repo source at `base_commit` on an ext4 disk right-sized to the checkout plus 64 MB headroom (typically a few MB–tens of MB). Attached as the second virtio block device; the base init mounts it at `/testbed`.

**Manifest** (`manifest.json`) — optional bucket-root index mapping each `instance_id` to local dev image paths (`base-images/`, `task-images/`). VM boot metadata also lives in `tasks.jsonl` for the manager catalog.

**Task dataset** (`tasks.jsonl`) — one line per instance with the index fields the trainer enumerates (`task_id`, `split`), the opening prompt (`messages`), tool schemas (`tools`), and node-relative VM image paths (`base_image`, `task_image`) for Firecracker boot. It carries no answer keys: the reward spec (held-out tests, test patch, test command) is baked into each task VM image at `/grl/task.json`, where only the in-VM scorer reads it.

### Prerequisites

- Docker (with buildx)
- [uv](https://docs.astral.sh/uv/)
- AWS CLI configured locally (`aws configure`) for uploads

### Usage

```bash
cd vms
uv sync

# full pipeline: generate dockerfiles, build images, upload to S3
export VMS_S3_BUCKET=my-bucket
export VMS_S3_REGION=us-west-2   # or set AWS_DEFAULT_REGION
uv run vms
```

Individual steps:

```bash
uv run vms generate          # write dockerfiles/ and manifest.json
uv run vms build             # build base-images/*.ext4 (skips existing)
uv run vms build-tasks       # build task-images/*.ext4 (skips existing)
uv run vms upload --jobs 4   # upload to s3://$VMS_S3_BUCKET/bases/ and .../tasks/
uv run vms tasks             # render tasks.jsonl (prompts + tools) for the trainer
uv run vms tasks --upload    # also upload to s3://$VMS_S3_BUCKET/datasets/swebench-lite/<split>/tasks.jsonl
uv run vms resolve <task_id>              # look up image paths from manifest.json
uv run vms resolve <task_id> --from-tasks tasks.jsonl  # from tasks.jsonl
```

Pass `--force` to rebuild or re-upload images that already exist. Use `--only <name>` with `build` or `build-tasks` to target a single image.

Uploads land at `s3://$VMS_S3_BUCKET/bases/<env>.ext4` and `s3://$VMS_S3_BUCKET/tasks/<instance_id>.ext4`. Existing objects with matching size are skipped unless `--force` is set, so failed or interrupted uploads can be retried. Uploads run in parallel with `--jobs`, or `VMS_UPLOAD_JOBS`; the default is 4. Large files use S3 multipart upload, so objects appear in the bucket only after all parts complete.

## env/

Rust `env` executor binary that runs inside each Firecracker VM. It implements this environment's tools (a persistent `bash` shell) and computes the task reward (`src/score.rs`): on `Score` it applies the held-out test patch, runs the targeted tests from the baked-in `/grl/task.json`, and returns reward 1.0 only if every `FAIL_TO_PASS` and `PASS_TO_PASS` test passes. It is baked into the VM images and invoked by the shared environment manager.

The environment-agnostic gRPC manager that handles VM lifecycle and tool call dispatch lives at [`environments/manager/`](../manager/) — it is shared across all environments and contains no swebench-lite-specific code.

```bash
# In-VM executor
cd env
cargo build
```

