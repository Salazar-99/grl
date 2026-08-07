---
name: grl
description: Operate GRL training clusters and runs with the grl CLI. Use when launching GRL workloads, provisioning or reusing a cluster, inspecting runs, or troubleshooting GRL deployment state.
---

# GRL CLI

Use `grl` from a shell. Keep the launch YAML in the project that owns the
experiment; do not put credentials or kubeconfig contents in run notes.

## Launching

Provision reusable EKS resources without starting training:

```bash
grl clusters create config.yaml --wait
```

Launch training. `infra.cluster_name` is the cluster identity: launch creates
an absent EKS target or reuses its local registered record. BYOK targets must
already be reachable through `launch.infra.kubeconfig`.

```bash
grl launch config.yaml --wait
```

Use in-memory overrides for an experiment rather than editing the YAML when
appropriate:

```bash
grl launch config.yaml --cluster grl-dev --env-bundle s3://bucket/bundle \
  --env-id swebench --env-split train --image-tag 0.2.0
```

Explicit `--head-image`, `--rollouts-image`, `--training-image`, and
`--manager-image` take precedence over automatic image resolution. A launch
refuses to overlap an active GRL RayJob on the selected Ray cluster; only use
`--replace-active` when cancelling that active job is intended.

## Inspecting state

```bash
grl clusters list
grl clusters status grl --output json
grl runs list --cluster grl
grl runs status RUN_ID
grl runs config RUN_ID              # effective redacted YAML
grl runs config RUN_ID --format json
```

Run records are local to the machine that launched them, under `~/.grl/runs`
(or `GRL_STATE_DIR`). Older launches may appear as `LAST_RUN` in `clusters
list` without a detailed run record. Do not assume that an empty `runs list`
means there is no live workload; use the cluster's Kubernetes tooling when a
live-state check is required.

## Safe workflow

Start with `--dry-run` for a new configuration. Use `clusters list` before
launching into a shared account, and inspect a failed run's effective config
and RayJob name before retrying. `grl teardown` destroys Terraform-managed
infrastructure; do not use it to stop a single training run.
