# Bring Your Own Kubernetes

BYOK lets GRL deploy its in-cluster stack into an existing Kubernetes cluster
instead of creating AWS networking, EKS, and node groups. The launcher uses the
Terraform root at `infra/byok` with `launch.infra.kubeconfig`.

BYOK only replaces cluster creation. The existing cluster must still satisfy the
same scheduling and runtime contract that the AWS root creates automatically.

## What the Launcher Applies

With BYOK enabled, the launcher:

1. Loads `launch.infra.kubeconfig` and uses that file's default/current context.
2. Runs the Terraform root at `infra/byok`.
3. Installs the shared `infra/modules/charts` operator releases:
   - OpenTelemetry Operator
   - NVIDIA GPU Operator
   - KubeRay Operator
4. Installs the shared `infra/modules/resources` Helm chart:
   - `RayCluster`
   - environment manager `DaemonSet` and `Service`
   - VM image cache and model cache `DaemonSet`s
   - OpenTelemetry collector resources
5. Applies the per-run Helm overlay during environment activation.
6. Submits training as a KubeRay `RayJob`.

## Launcher Configuration

Minimal BYOK launcher settings:

```yaml
launch:
  infra:
    apply: false
    apply_cluster: false
    apply_byok: true
    byok_terraform_dir: infra/byok
    kubeconfig: ~/.kube/config
    auto_kubeconfig: false
```

The kubeconfig's selected context is used for Terraform, Helm, and Kubernetes
API calls. There is no separate context setting.

## Required Node Labels

The GRL chart schedules pods using node labels. Your cluster must provide nodes
with these labels:

| Label | Used by |
| --- | --- |
| `role=ray` | Ray head pod |
| `role=rollouts` | Ray rollout worker group |
| `role=training` | Ray training worker group |
| `role=environment` | environment manager and VM image cache |

Environment nodes must also be capable of exposing `/dev/kvm` to privileged
pods. The AWS root labels these nodes with `kvm=true`; BYOK clusters should do
the same for clarity, even though the current chart selects by `role=environment`.

## GPU Requirements

Rollout and training workers run on GPU nodes. BYOK clusters must provide:

- Kubernetes nodes labeled `role=rollouts` and `role=training`
- NVIDIA GPUs allocatable as `nvidia.com/gpu`
- GPU scheduling support, either from the NVIDIA GPU Operator installed by GRL
  or from equivalent pre-existing cluster setup
- The `nvidia.com/gpu=true:NoSchedule` taint if you want to match the default
  chart tolerations and AWS behavior

The Ray worker groups advertise custom Ray resources:

- rollout workers advertise `{"rollouts": gpusPerNode}`
- training workers advertise `{"training": gpusPerNode}`

The Python actors require those logical resources:

- `RolloutWorker`: `@ray.remote(num_gpus=1, resources={"rollouts": 1})`
- `TrainingWorker`: `@ray.remote(num_gpus=1, resources={"training": 1})`

Set these values correctly for BYOK, because AWS instance-type GPU detection is
not available:

```yaml
infra:
  ray_cluster:
    workers:
      rollouts:
        gpus_per_node: 1
      training:
        gpus_per_node: 1
```

## KVM and Environment Nodes

Environment manager pods launch Firecracker microVMs and require:

- nodes labeled `role=environment`
- host `/dev/kvm` support
- privileged pods allowed by cluster policy
- enough CPU and memory for concurrent microVMs
- writable host path at `infra.vm_image_cache.host_path` (default `/var/lib/grl`)

If `infra.vm_image_cache.bucket` is set, the VM image cache `DaemonSet` syncs
Firecracker VM artifacts from S3 onto environment nodes. BYOK clusters must
provide credentials that allow those pods to read the bucket, for example via
node identity, workload identity, or another cluster-specific mechanism.

## Storage and Caches

The chart uses host paths for hot-path local caches:

- VM artifacts: `infra.vm_image_cache.host_path` (default `/var/lib/grl`)
- model weights: `infra.model_cache.host_path` (default `/models`)

If model caching is enabled, nodes labeled `role=rollouts` and `role=training`
must have enough local disk for the model weights. If the model requires a
Hugging Face token, set `infra.model_cache.huggingface_token`.

## Networking

Pods must be able to reach:

- Kubernetes API server from Terraform/Helm via the kubeconfig
- container registries for GRL images and operator images
- S3 or equivalent object storage for environment bundles and VM artifacts
- Hugging Face Hub if model cache is enabled and weights are not already local
- the configured external OTLP endpoint, if telemetry export is enabled

In-cluster service names are assumed to resolve normally through Kubernetes DNS,
for example `grl-manager.default.svc:50051` and
`ray://grl-ray-head:10001`.

## What BYOK Does Not Create

BYOK does not create:

- cloud VPCs, subnets, NAT gateways, or load balancers
- Kubernetes control planes
- node groups or autoscaling groups
- cloud IAM roles or bucket policies
- node labels, node taints, or GPU/KVM hardware

Those are owned by the user-provided cluster.

## Common Failure Modes

- Ray pods stay pending: missing `role=*` labels, missing GPUs, mismatched taints,
  or insufficient CPU/memory.
- Ray actors stay pending: the Ray worker group did not advertise the custom
  `rollouts` or `training` resource, usually because `gpus_per_node` is wrong or
  the worker pod did not start.
- Training fails on CUDA: GPU device plugin/operator is missing or the pod did
  not receive `nvidia.com/gpu`.
- Manager pods fail: environment nodes do not expose `/dev/kvm`, privileged pods
  are blocked, or the VM cache host path is unavailable.
- VM cache fails: pods cannot read the configured VM image bucket.
- Model cache fails: insufficient disk, missing Hugging Face credentials, or no
  outbound access to Hugging Face.
