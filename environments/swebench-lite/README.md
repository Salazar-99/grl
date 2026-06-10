# SWEBench-Lite Environment for grl

This environment consists of a raw `data` directory, a `vms` module to generate firecracker vm images for each problem in the dataset, and an environment management `server` to manage the environments during training and provide an interaction mechanism for the agent rollouts.

## data/
This directory just contains the SWEBench-Lite dataset cloned [from HuggingFace here](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/main).
This is used by the `vms/` module to generate the Firecracker VM environments for use in training.

## vms/
Python tooling that builds Firecracker-ready ext4 disks for the SWE-bench-lite dev split.

**Base images** (`base-images/`) — one per repo+version environment. Ubuntu, Python, and pip dependencies are baked in via Docker, then exported to an ext4 disk sized to fit the content plus 512 MB of runtime headroom (typically ~1–1.8 GB).

**Task images** (`task-images/`) — one per dataset instance. Repo source at `base_commit` on an ext4 disk right-sized to the checkout plus 64 MB headroom (typically a few MB–tens of MB).

**Manifest** (`manifest.json`) — maps each `instance_id` to its base and task image paths.

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
uv run vms resolve <task_id> # look up image paths for a task
```

Pass `--force` to rebuild or re-upload images that already exist. Use `--only <name>` with `build` or `build-tasks` to target a single image.

Uploads land at `s3://$VMS_S3_BUCKET/bases/<env>.ext4` and `s3://$VMS_S3_BUCKET/tasks/<instance_id>.ext4`. Existing objects with matching size are skipped unless `--force` is set, so failed or interrupted uploads can be retried. Uploads run in parallel with `--jobs`, or `VMS_UPLOAD_JOBS`; the default is 4. Large files use S3 multipart upload, so objects appear in the bucket only after all parts complete.

## server/

Rust binaries for managing Firecracker VMs and executing tools inside them.

- `server` — gRPC server called by training workers (`EnvironmentService`)
- `executor` — runs inside the VM (placeholder)

The gRPC contract lives in [`proto/grl/environment/v1/environment.proto`](../../proto/grl/environment/v1/environment.proto) at the repo root.

```bash
# Rust server
cd server
cargo run --bin server

# Regenerate Python stubs after changing the proto
cd ../../training
uv sync --group dev
uv run generate-proto
```

Set `GRL_ENV_SERVER_ADDR` (default `0.0.0.0:50051` on the server, `localhost:50051` in Python) to point clients at the server.

