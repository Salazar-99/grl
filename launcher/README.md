# GRL CLI

Single command to provision infrastructure, activate an environment bundle, and submit a training job on the GRL Ray cluster.

## Install

```bash
cd launcher
uv sync
uv pip install -e .
```

Or from anywhere:

```bash
uvx --from /path/to/grl/launcher grl launch config.yaml
```

## Quick start

```bash
grl init config.yaml
# edit config.yaml (AWS creds, bundle_uri, infra settings)
grl launch config.yaml
```

Dry run:

```bash
grl launch config.yaml --dry-run
```

Preflight only:

```bash
grl launch config.yaml --preflight-only
```

## Prerequisites

- Python 3.12
- AWS credentials configured locally (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or SSO/profile)
- For `images.mode: published` (default): published GRL container images in your registry
- For `launch.infra.apply: true`: permission to run Terraform against your AWS account

The CLI auto-downloads pinned Terraform, Helm, and kubectl binaries into `~/.cache/grl/tools` when `launch.tools.auto_install` is true.

## Config layers

One YAML drives all layers:

| Section | Purpose |
|---------|---------|
| `model`, `grpo`, `workers`, ... | Training hyperparameters (passed to Ray job) |
| `environment` | Bundle URI, split, manager address |
| `launch` | Which layers to run (infra apply, env activate, job submit) |
| `images` | Runtime image resolution (`published`, `custom`, `build_and_push`) |
| `infra` | Cluster, Helm, and Terraform settings |

Secrets can use environment variable references:

```yaml
infra:
  otel_collector:
    upstream:
      password: "${env:OTEL_UPSTREAM_PASSWORD}"
```

## Image modes

**published** (default): resolve `auto` refs from `images.registry` and `images.tag`.

**custom**: use explicit image refs under `images.training` and `images.manager`.

**build_and_push**: build and push Docker images from a GRL checkout, then deploy them.

## Job submission

Training runs are submitted as KubeRay `RayJob` custom resources. The launcher:

1. Writes a training-only config (no `infra` / `launch` keys) to a ConfigMap
2. Creates a `RayJob` targeting the existing `RayCluster`
3. Optionally waits for job completion when `launch.job.wait: true`

## Tools

```bash
grl tools doctor
grl tools install
grl tools list
```

## Development

```bash
cd launcher
uv sync --group dev
uv run pytest
```

Run from a GRL repo checkout so Terraform templates and the Helm chart resolve from `infra/`.
