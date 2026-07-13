# Managed Environment Contract

This document defines the boundary between GRL's generic Firecracker VM layer
and an environment-specific guest package. The manager treats environment
packages as opaque artifacts; adding an environment must not require adding its
task, tool, or scoring semantics to the manager or bootstrap.

## Ownership boundary

The generic VM layer owns:

- selecting and loading the kernel and external initramfs;
- attaching the base, task, environment, and scratch block devices;
- mounting `/proc`, `/sys`, `/dev`, and `/dev/pts`;
- assembling a writable OverlayFS root from a read-only base and private ext4
  scratch;
- keeping the initramfs bootstrap as PID 1 and supervising one chrooted
  environment process group;
- Firecracker lifecycle, vsock transport, snapshot/restore, cancellation, and
  teardown;
- classifying failures before the environment protocol is available as
  infrastructure errors.

The environment package owns:

- interpreting the task payload and preparing its workspace;
- the tools exposed to the policy and their persistent session behavior;
- submission semantics;
- reward evaluation and environment-specific failure details;
- cleanup of environment-owned processes and files.

Generic boot code must not contain environment paths or concepts such as
`/testbed`, `task.json`, pytest, SWE-bench patches, or reward semantics.

## Artifact and drive contract

All artifacts are immutable. Their selected S3 keys are activated atomically in
the node cache.

An environment bundle contains `tasks.jsonl`, `environment.squashfs`,
`environment.squashfs.sha256`, and `environment-manifest.json`. The manifest
has `schema_version`, `environment`, `entrypoint`, `protocol_version`, and
`sha256` fields. Bundle sync verifies the checksum before publishing the
package. Each complete bundle is stored under a content-derived directory and
one `active` symlink is switched atomically. Catalog generations canonicalize
that symlink so tasks stay pinned to their matching environment package. The
manager resolves the package independently of the base and task images.
Version directories must not be removed by bundle sync; future garbage
collection requires manager acknowledgement that no catalog generation,
in-flight boot, or snapshot still references them.

| Device | Artifact | Access | Guest use |
| --- | --- | --- | --- |
| `/dev/vda` | Base SquashFS | read-only, non-root | OverlayFS lower |
| `/dev/vdb` | Task SquashFS | read-only | mounted at `/run/grl/task` |
| `/dev/vdc` | Environment SquashFS | read-only | mounted at `/run/grl/environment` |
| `/dev/vdd` | Per-rollout ext4 scratch | read-write | OverlayFS upper/work |

The kernel loads the global `bootstrap/active.cpio.gz` as its initramfs.
`grl-bootstrap` remains PID 1 in that initramfs. It mounts the base and private
scratch as an OverlayFS at `/sandbox-root`, recursively bind-mounts `/proc`,
`/sys`, and `/dev` into it, and mounts task and environment packages below
`/sandbox-root/run/grl`. It then forks a child into a private mount namespace,
chroots that child to `/sandbox-root`, and executes:

```text
/run/grl/environment/entrypoint
```

The bootstrap and environment package are mandatory. The manager must reject a
boot when either is absent; base images do not contain a fallback `/init` or
environment executable.

The entrypoint must be executable. It may be dynamically linked against
libraries in the selected base image. The bootstrap itself is statically linked
because it runs before the base filesystem is mounted.

## Entrypoint lifecycle

The entrypoint:

1. Reads task data from the read-only `/run/grl/task` mount.
2. Creates all mutable workspace state in the writable root or scratch.
3. Opens a guest vsock listener on port `5005`.
4. Serves the framed GRL environment protocol until terminated.
5. Leaves no task mutation in the task or environment SquashFS.

The bootstrap remains PID 1, forwards `SIGTERM` and `SIGINT` to the entrypoint,
reaps all orphaned descendants, terminates the remaining process group after
the entrypoint exits, and exits with the entrypoint's status. The workload is
not given a second PID namespace; the microVM is already its PID-isolation
boundary.

Readiness means the manager can complete a new vsock connection and handshake
with the entrypoint. A process merely existing is not readiness.

## Protocol contract

The environment entrypoint uses the protobuf messages in
`proto/grl/environment/v1/environment.proto`. On vsock, each request is:

```text
1-byte message kind | 4-byte big-endian length | protobuf payload
```

Replies are a 4-byte length followed by the corresponding protobuf response.
One connection may carry many requests. Implementations must support:

- kind `0`: `ExecuteRequest` to `ExecuteResponse`;
- kind `1`: `EvaluateRequest` to `EvaluateResponse`.

Protocol changes must remain backward compatible or increment an explicit
protocol version included in the environment artifact manifest and snapshot
cache key.

## Errors and evaluation

Transport loss, malformed frames, unavailable boot artifacts, mount failures,
snapshot failures, and an unreachable entrypoint are infrastructure errors.

Tool command failures are normal `ExecuteResponse` values with `is_error=true`;
they are not manager transport failures. Evaluation must return structured
detail JSON. Errors showing that the scorer itself could not run are
infrastructure errors; a valid score of zero is not.

## Snapshot contract

A golden snapshot may be taken only after task preparation and listener
readiness, but before any rollout tool session exists. Its cache key includes:

- kernel, bootstrap, base, task, and environment artifact contents;
- Firecracker version;
- vCPU, memory, boot arguments, and snapshot format version.

Restored clones share the immutable memory file through `MAP_PRIVATE`, receive a
private reflinked scratch disk, and use a unique host vsock UDS. The environment
must tolerate Firecracker's vsock transport reset: existing connections close,
while the listener must accept a new connection after resume.

An incompatible or corrupt snapshot is discarded. Cold boot remains the
correctness fallback.

## Host jail contract

Direct and Firecracker-jailer launches use the same guest contract. In jailed
mode, the manager creates one sanitized per-VM jail root, hardlinks (or copies
across filesystems) every immutable kernel/initrd/drive artifact into it, and
creates scratch, API socket, vsock UDS, and snapshot staging files there.
Firecracker API paths are chroot-relative; manager transport paths are the
corresponding host paths. Teardown reaps Firecracker before removing the jail.
`manager.useJailer` must be promoted only after the KVM conformance workflow
passes in both direct and jailed modes.

## Minimal environment

A minimal package contains:

```text
entrypoint
```

The entrypoint may copy or interpret its task payload however it chooses, but it
must eventually listen on vsock port `5005` and implement the protocol above.
It must not depend on files privately baked into the bootstrap.

## Conformance checklist

- The environment SquashFS is immutable and has an executable `/entrypoint`.
- The entrypoint works with only the documented mounts and environment
  variables.
- A cold boot reaches vsock readiness.
- At least one tool request and one evaluation request round-trip correctly.
- Tool state persists within one rollout but is absent in a fresh rollout.
- Two concurrent restored clones cannot observe each other's workspace writes.
- The listener reconnects after snapshot restore.
- `/dev/pts` permits `openpty`, and task/environment mounts are visible inside
  the chroot.
- A zero reward is distinguishable from scorer infrastructure failure.
- `SIGTERM` stops the entrypoint and descendants without zombies.
- Changing any relevant artifact invalidates the golden snapshot.
