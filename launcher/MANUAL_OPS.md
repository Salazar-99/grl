# Manual ops gaps (candidates for launcher automation)

Notes from rolling a published manager image (`0.0.8` → `0.0.9`) and starting a new training run on an existing EKS cluster. Items below had to be done by hand; fold them into `grl launch` when practical.

## Goal of this roll

Ship a manager fix (Firecracker API keep-alive deadlock in `put`) without tearing down the cluster: bump published tag → re-apply RESOURCES → cancel old job → submit TRAINING.

## Procedure used (2026-07-11)

1. **Publish images** — tag `v0.0.9` and push; GitHub Actions workflow `Publish Images` builds `ghcr.io/salazar-99/grl-{manager,training-*}:0.0.9`.
2. **Bump config** — set `images.tag: "0.0.9"` in `launcher/config.yaml`.
3. **Stop the in-flight job** (manual):
   ```bash
   kubectl delete rayjob -n default <running-rayjob-name> --wait=false
   ```
   Do **not** run `grl teardown` — that destroys Terraform-managed infra.
4. **Re-apply RESOURCES**:
   ```yaml
   launch:
     deployment_type: RESOURCES
   ```
   ```bash
   uv run --directory launcher grl launch /path/to/launcher/config.yaml
   ```
   Helm updates manager + Ray worker images; manager DaemonSet uses `imagePullPolicy: Always` and rolls to the new tag.
5. **Submit TRAINING**:
   ```yaml
   launch:
     deployment_type: TRAINING
   ```
   ```bash
   uv run --directory launcher grl launch /path/to/launcher/config.yaml
   ```
   Assumes ENVS (bundle-sync) is already healthy from a prior activate.

## Manual steps / launcher gaps

| Gap | What we did | Suggested launcher behavior |
|-----|-------------|-------------------------------|
| **`launch.job.force` is unused** | Config has `job.force: false` but `submit_training_job` never cancels an existing RayJob. Had to `kubectl delete rayjob …` before TRAINING. | If `job.force: true` (or always when submitting a new run_id), delete/stop RayJobs in the namespace that target this Ray cluster (or only RUNNING ones), then create. Document that `force: false` fails fast on conflict instead of replace-in-place. |
| **Stopping a run ≠ teardown** | Easy to confuse with `grl teardown`. | Add `grl launch --cancel` / `grl job cancel` that only deletes RayJob(s), or make TRAINING with `force` the supported path. |
| **Image tag bump requires config edit** | Hand-edited `images.tag` to `0.0.9`. | Optional CLI override: `grl launch --image-tag 0.0.9`, or read tag from `git describe` / env when `images.mode: published`. |
| **Layer flip requires two launches** | RESOURCES then TRAINING as two config edits + two `grl launch` invocations. | Support `deployment_type: RESOURCES,TRAINING` or a `roll_images_and_train` path that reapplies resources then submits without rewriting YAML twice. |
| **Old FAILED RayJobs accumulate** | Left failed jobs from earlier runs; only deleted the RUNNING one. | On TRAINING submit (or a `grl job gc`), delete FAILED/STOPPED RayJobs older than N, or all but the latest. |
| **Stuck / Evicted manager pods** | Previously (disk pressure) Evicted pods blocked the DaemonSet (`READY 0/1`) until manually deleted. | After RESOURCES apply, wait for manager DS ready; if Failed/Evicted pods with the DS label exist, delete them and re-check rollout. |
| **Stuck Terminating cache pods** | `vm-image-cache` sometimes stuck Terminating after scratch_gb / hostPath changes; needed `--force --grace-period=0`. | On RESOURCES / cache DaemonSet update timeout, detect Terminating>T and force-delete; surface a clear error if PVC/hostPath finalizers block. |
| **RayJob replace on name conflict** | `create_rayjob` replaces on 409, but a still-RUNNING job of a *different* `run_id` is left alone; capacity/GPU contention remains. | Cancel other RUNNING RayJobs for the same `ray.io/cluster` when `force` is set, not only replace same-name. |
| **No post-roll readiness gate for manager API** | Relied on DS Ready + later TRAINING traffic. | After manager image change, optional probe: gRPC `ListTasks` or catalog non-empty before submit. |
| **RayCluster image bump does not restart pods** | Helm set RayCluster images to `0.0.9`, but head/worker pods kept running `0.0.8` until manually deleted. Manager DS *did* roll (template change + Always pull). | After RESOURCES apply, if Ray pod images ≠ CR images, delete head/worker pods (or annotate restart) and wait Ready before TRAINING. Prefer a KubeRay-supported rolling update if available. |
| **Config path with `uv run --directory launcher`** | Relative `launcher/config.yaml` fails because cwd is `launcher/`. | Document absolute path, or resolve config relative to repo root / caller cwd. |

## What the launcher already does correctly

- `images.mode: published` + `images.tag` → resolves `ghcr.io/…/grl-manager:<tag>` and training role images.
- RESOURCES Terraform/Helm apply updates `manager.image` and RayCluster images in one shot.
- Manager chart sets `imagePullPolicy: Always`, so a tag change rolls pods without a manual restart.
- Per-VM run state lives under container `/var/run/grl/vms` (not the VM cache hostPath), so a manager pod roll clears stuck Firecracker sockets from a bad build.

## Verify after roll

```bash
kubectl get ds grl-manager -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
# expect: …/grl-manager:0.0.9

kubectl get pods -l app=grl-manager
kubectl logs -l app=grl-manager --tail=20
# expect: listening … catalog …

kubectl get rayjob -n default
# expect: new grl-run-* RUNNING (or after submit)
```

Healthy boot signal (vs the keep-alive bug): manager run dirs gain `vsock.sock` after API config, and executes stop returning perpetual `UNAVAILABLE` for NotReady envs.

## Out of scope / do not automate casually

- `grl teardown` — full cluster destroy; never use to stop a single training run.
- Force-deleting arbitrary pods without a timeout/stuck predicate.
- Republishing images (CI on git tag remains the source of truth).
