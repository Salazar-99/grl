# GRL Launcher — implementation plan

The launcher binds a **per-run environment** to the long-lived cluster (step 4 of the
env-activation design). Steps 1–3 are implemented elsewhere:

| Step | What | Where |
|------|------|--------|
| 1 | Manager DaemonSet syncs `bundle_uri` → node cache at pod start | `infra/.../manager.yaml` initContainer |
| 2 | Manager loads catalog from `GRL_TASKS_FILE` at startup | `environments/manager/` |
| 3 | Trainer calls `ListTasks` instead of S3 | `training/environments.py`, `main.py` |
| **4** | **Launcher CLI orchestrates activation + Ray job submit** | **this directory (TBD)** |

Terraform provisions env-agnostic machinery. The launcher is the only component that
knows which environment a given training run uses.

---

## Responsibilities

```
grl launch run @ path/to/run-config.yaml

  1. Validate run config (training YAML + environment block)
  2. Resolve bundle_uri → head S3 objects exist (tasks.jsonl, manifest.json)
  3. Optional: drain or cancel prior Ray training job on the cluster
  4. (Optional) Purge stale ext4s on environment nodes — see below
  5. Helm upgrade: manager.bundleUri, manager.envId, vmImageCache.bucket (if changed)
  6. Rolling restart vm-image-cache DaemonSet (if bucket or new ext4s)
  7. Rolling restart grl-manager DaemonSet; wait for ready
  8. Verify manager catalog: ListTasks count matches expectation
  9. Submit Ray job with training config (no bundle_uri needed at runtime)
  10. Write run metadata (ConfigMap or local state file)
```

---

## Proposed layout

```
launcher/
  PLAN.md                 # this file
  pyproject.toml            # uv package: grl-launcher
  src/grl_launcher/
    __init__.py
    cli.py                  # `grl launch run`, `grl launch activate-env`
    config.py               # reads training YAML environment block + launcher opts
    bundle.py               # S3 head/list, manifest parse, ext4 sync plan
    k8s.py                  # kubectl/helm patch, rollout status, ray job submit
    verify.py               # grpc ListTasks health check
```

Keep the launcher **outside** `training/` so cluster ops deps (kubectl, helm, boto3)
do not land on rollout/training GPU images.

---

## CLI surface (v1)

### `grl launch activate-env`

Activates an environment on the cluster without starting training. Useful for
preflight and switching envs between experiments.

```bash
grl launch activate-env \
  --bundle-uri s3://bucket/datasets/swebench-lite/dev \
  --env-id swebench-lite \
  --split dev \
  --wait
```

Steps: 4–8 above only.

### `grl launch activate-env --purge-cache`

Same as `activate-env`, but removes stale task (and optionally base) ext4s from
environment nodes before restarting DaemonSets. See **Switching environments**.

### `grl launch run`

Full training run.

```bash
grl launch run @ configs/swebench-lite-dev.yaml \
  --wait-manager \
  --ray-address ray://grl-ray-head:10001
```

Reads `environment.bundle_uri`, `environment.id`, `environment.split` from the
training config. Calls `activate-env`, then `ray job submit` with the same YAML.

Flags:

| Flag | Purpose |
|------|---------|
| `--skip-env-activate` | Ray job only; manager already on correct bundle |
| `--force` | Cancel in-flight Ray job before env switch |
| `--purge-cache` | Remove old ext4s from `/var/lib/grl/images/` before vm-image-cache restart |
| `--dry-run` | Print kubectl/helm/s5cmd commands without executing |

---

## Kubernetes integration

### Patch manager DaemonSet env (v1 — no Helm release required)

The chart templates already wire:

```yaml
manager:
  bundleUri: ""   # → GRL_BUNDLE_URI in initContainer + manager
  envId: ""       # → GRL_ENV_ID
  activeDir: active
```

Launcher options (pick one for v1):

**A. `kubectl patch daemonset`** (fastest to ship)

Patch pod template env `GRL_BUNDLE_URI` / `GRL_ENV_ID`, then
`kubectl rollout restart daemonset/grl-manager`.

**B. Helm upgrade with values file** (cleaner long-term)

Launcher writes `/tmp/grl-run-values.yaml` overlay:

```yaml
manager:
  bundleUri: s3://bucket/datasets/swebench-lite/dev
  envId: swebench-lite
```

`helm upgrade grl-resources ./chart -f values.yaml -f /tmp/grl-run-values.yaml`

Prefer **B** when switching environments: one overlay file records the full S3
binding for the run. Use **A** only for quick dev iteration.

---

## Switching environments (no cluster teardown)

Reuse the same EKS cluster and Ray cluster for a **completely different**
environment by rebinding S3 and restarting DaemonSets on environment nodes.
Terraform and the Ray cluster do not need to change.

### Two Helm values, two DaemonSets

Environment nodes use `/var/lib/grl/` (`vmImageCache.hostPath`). Two independent
S3 bindings and two DaemonSets refresh different parts of that tree:

| Helm value | Example | DaemonSet | Synced into |
|------------|---------|-----------|-------------|
| `manager.bundleUri` | `s3://bucket/datasets/new-env/dev` | **`grl-manager`** | `active/tasks.jsonl`, `active/manifest.json` |
| `vmImageCache.bucket` | `my-bucket` (name only, not a prefix) | **`vm-image-cache`** | `kernel/`, `images/bases/`, `images/tasks/`, root `manifest.json` |

Training config `environment.bundle_uri` must match `manager.bundleUri`. When the
new env lives in a **different bucket**, patch **both** values in the Helm overlay.

### Launcher overlay (example)

```yaml
# /tmp/grl-run-values.yaml — written per run
manager:
  bundleUri: s3://new-bucket/datasets/my-env/dev
  envId: my-env
vmImageCache:
  bucket: new-bucket   # omit or unchanged if same bucket as before
```

```bash
helm upgrade grl-resources infra/modules/resources/chart \
  -f infra/modules/resources/chart/values.yaml \
  -f /tmp/grl-run-values.yaml
```

### When to restart which DaemonSet

| Scenario | Patch | Restart |
|----------|-------|---------|
| Same bucket, new catalog path (new split / env bundle) | `manager.bundleUri`, `manager.envId` | **`grl-manager` only** |
| Same bucket, new task/base ext4s uploaded | (bucket unchanged) | **`vm-image-cache`**, then **`grl-manager`** |
| Different S3 bucket | `vmImageCache.bucket` + `manager.bundleUri` | **both** (vm-image-cache first) |

InitContainers run s5cmd sync **once per pod start**. A rollout restart is required
for new S3 content to be pulled; updating Helm values alone does not re-sync until
pods are recreated.

```bash
# Order when both need refresh:
kubectl rollout restart daemonset/vm-image-cache -n default
kubectl rollout status  daemonset/vm-image-cache -n default

kubectl rollout restart daemonset/grl-manager -n default
kubectl rollout status  daemonset/grl-manager -n default
```

Wait for `vm-image-cache` before `grl-manager` when new ext4s are involved — the
manager will eventually boot VMs from paths under `/var/lib/grl/images/`.

### Removing old ext4s

`s5cmd sync` is **additive**: it downloads new/changed objects but **does not delete**
files left over from a previous environment. Old task disks can accumulate under
`/var/lib/grl/images/tasks/` (and unused bases under `images/bases/`).

**When to purge**

- Switching to a completely different environment (different task IDs / manifest).
- Environment nodes are disk-constrained.
- You need a clean cache for debugging or reproducibility.

**When skip is OK**

- Same SWE-bench-lite bucket, new split only (catalog change); task ext4s largely overlap.
- Additive cache growth is acceptable.

**Launcher purge strategy (v1)**

Run a Kubernetes Job (or SSH/node script) on each environment node with
`hostPath: /var/lib/grl` and `nodeSelector: role=environment`:

```bash
# Conservative: drop all task disks; keep shared bases and kernel
rm -f /var/lib/grl/images/tasks/*.ext4

# Aggressive: full image cache reset (forces full re-sync on next vm-image-cache start)
rm -rf /var/lib/grl/images/tasks/* /var/lib/grl/images/bases/*
# kernel/ is usually env-agnostic; leave unless switching Firecracker kernel version
```

Implement as `grl launch activate-env --purge-cache`:

1. Drain / cancel Ray job (`--force`).
2. Run purge Job on environment nodes (task ext4s; optional `--purge-bases`).
3. Helm upgrade with new overlay.
4. Restart `vm-image-cache` (re-sync from S3).
5. Restart `grl-manager` (re-sync bundle → `active/`).
6. `ListTasks` verify → done (or continue to `grl launch run`).

**Future:** manifest-aware purge — delete only ext4s present in the *previous* run's
manifest but absent from the new one, instead of wiping all of `tasks/`.

### Full env-switch sequence (reference)

```
1. Drain / cancel in-flight Ray training job
2. (Optional) --purge-cache on environment nodes
3. helm upgrade with run overlay (bundleUri, envId, bucket if changed)
4. kubectl rollout restart daemonset/vm-image-cache   # skip if catalog-only change
5. kubectl rollout restart daemonset/grl-manager      # always
6. ListTasks verify
7. ray job submit
```

One active catalog per cluster in v1: do not start a new run on a different
`bundle_uri` until steps 1 and 5 complete.

---

### Task ext4 sync (manifest subset)

`vm-image-cache` syncs global `kernel/`, `bases/`, and all of `tasks/`. For a new
env or split, launcher should sync **only** ext4 paths listed in `manifest.json`:

```
s3://{bucket}/tasks/{instance_id}.ext4  →  /var/lib/grl/images/tasks/
```

Implementation: Kubernetes Job with `hostPath` + `nodeSelector: role=environment`,
running s5cmd per node OR extend manager initContainer to also sync manifest entries
from the bucket root (not just `bundle_uri/*`).

Recommendation: separate **`grl-sync-task-images` Job** in v1 so manager init stays
fast (tasks.jsonl + manifest only).

### Run metadata ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grl-active-run
data:
  env_id: swebench-lite
  bundle_uri: s3://...
  split: dev
  run_id: ...
  task_count: "123"
  activated_at: "2026-06-24T..."
```

Trainer can optionally verify `ListTasksResponse.env_id` matches `environment.id`.

---

## Config contract

Training YAML (`training/config.yaml` shape):

```yaml
environment:
  id: swebench-lite
  bundle_uri: s3://my-bucket/datasets/swebench-lite/dev  # launcher only
  split: dev
  server_addr: grl-manager.default.svc:50051
```

| Field | Consumer |
|-------|----------|
| `bundle_uri` | Launcher → manager initContainer |
| `id` | Launcher → `GRL_ENV_ID`; optional trainer verify |
| `split` | Trainer → `ListTasks` filter |
| `server_addr` | Trainer → all gRPC calls |

Trainer **never** reads `bundle_uri` at runtime.

---

## Verification (`verify.py`)

After manager rollout:

```python
task_ids = await list_task_ids(addr=server_addr, split=split)
assert len(task_ids) > 0
# optional: assert response.env_id == config.environment.id
```

Use grpc health or retry with backoff (catalog loads synchronously at manager start). Reuse ``list_task_ids`` from ``training.environments``.

---

## Ray job submit (`k8s.py`)

```bash
ray job submit \
  --address "ray://grl-ray-head:10001" \
  --working-dir training/ \
  --runtime-env-json '{"pip": ...}' \
  -- python -m training.main --config /path/to/run-config.yaml
```

Mount or copy run config into the Ray head pod, or bake config into a ConfigMap
volume on the head group.

Open question: config delivery mechanism (ConfigMap vs S3 vs inline env). **ConfigMap**
is simplest for v1.

---

## Error handling

| Failure | Behavior |
|---------|----------|
| Prior Ray job running | `--force` cancels; default waits with timeout |
| Manager rollout timeout | Abort; do not submit Ray job |
| ListTasks empty | Abort; log bundle_uri and split |
| Partial ext4 sync | Retry Job; s5cmd skips existing objects |

One active env per cluster in v1: always restart manager before a new run with a
different `bundle_uri`.

---

## Dependencies

- `kubectl` (cluster access)
- `helm` (optional, v2)
- `ray` CLI or Ray Jobs API
- `boto3` / `s5cmd` for bundle validation and task-image sync
- `grpcio` for ListTasks verify (reuse generated stubs or thin client)

---

## Implementation order

1. **`bundle.py`** — parse manifest, validate S3 objects, build sync command list
2. **`k8s.py`** — helm upgrade overlay; rollout restart vm-image-cache + grl-manager; poll ready
3. **`verify.py`** — ListTasks gRPC check
4. **`cli.py activate-env`** — wire 1–3 (purge, dual DaemonSet restart, verify)
5. **`k8s.py ray submit`** — ConfigMap config + job submit
6. **`cli.py run`** — end-to-end
7. **Tests** — unit tests for manifest parsing; optional kind integration test

---

## Out of scope (v1)

- Multi-env concurrent catalogs on one manager
- Hot catalog reload without pod restart
- Terraform changes per run
- Building/uploading bundles (stays in `environments/swebench-lite/vms`)

---

## Related files

- Manager chart: `infra/modules/resources/chart/templates/manager.yaml`
- VM cache chart: `infra/modules/resources/chart/templates/vm-image-cache.yaml`
- Env flow notes: `NOTES.md`
- Proto: `environments/proto/grl/environment/v1/environment.proto` (`ListTasks`)
- Trainer client: `training/src/training/environments.py`
- Training config: `training/src/training/config.py`
