# Phase 3 — Read-only squashfs images + per-VM overlay scratch

## Goal

Ship base and task disk images as **compressed, read-only squashfs** lowers and
give each microVM its own **writable ext4 scratch** disk (copied from a
pre-formatted node-local template) at boot. This:

- Shrinks shipped/cached artifacts ~6–8× (base 2000 MiB → ~250–350 MiB; task
  150 MiB → single-digit MiB) by compressing content and dropping baked-in
  headroom.
- Makes read-only lowers **shareable** across concurrent VMs (GRPO fans out
  `num_rollouts` VMs per `task_id`), sharing host page cache.
- Structurally removes today's RW-corruption hazard: multiple VMs currently
  point at the same `*.ext4` cache file with `is_read_only:false`
  (`vm/mod.rs::boot` does not copy per-VM). Squashfs cannot be mounted RW, so
  immutability is a property of the format, not a flag.

This is a greenfield change — **there is no legacy `.ext4` boot path to keep**.
The manager, guest init, and build tooling target squashfs+overlay only.

## Verified prerequisites (guest kernel `Linux 6.1.155+`)

Confirmed from the extracted `IKCONFIG`:

- `CONFIG_OVERLAY_FS=y`, `CONFIG_SQUASHFS=y`
- Squashfs decompressors `ZSTD`/`XZ`/`ZLIB`/`LZ4`/`LZO` all `=y` → build with
  `mksquashfs -comp zstd` (fast + high ratio).
- `CONFIG_VIRTIO_BLK=y`, `CONFIG_VIRTIO_MMIO=y`, `CONFIG_EXT4_FS=y`,
  `CONFIG_DEVTMPFS_MOUNT=y`, `CONFIG_PROC_FS=y`, `CONFIG_SYSFS=y`.
- Directory-rename note: `CONFIG_OVERLAY_FS_REDIRECT_DIR` is off, which would
  `EXDEV` on renaming a directory that lives only in a read-only lower. **We do
  not hit this** — see Architecture: `/testbed` is a real writable copy (not an
  overlay of the task lower), and the rootfs overlay never needs directory
  renames. No kernel rebuild required.

## Architecture

Three drives per VM:

| Guest dev | Layer   | Backing file (host)                          | Mode | Role |
|-----------|---------|----------------------------------------------|------|------|
| `/dev/vda`| base    | `{cache}/images/bases/<repo-ver>.squashfs`   | RO   | rootfs lower |
| `/dev/vdb`| task    | `{cache}/images/tasks/<task_id>.squashfs`    | RO   | source for `/testbed` copy |
| `/dev/vdc`| scratch | `{run_dir}/<env_id>/scratch.ext4`            | RW   | overlay upper for `/`, holds `/testbed` |

Scratch is a **per-VM copy of a pre-formatted, journal-less ext4 template**
staged once per node (fast boot, no in-guest `mkfs`). `grl-init` identifies
drives by `blkid` TYPE (squashfs vs ext4), not device order.

Boot flow:
1. Firecracker boots the base squashfs as root device (`root=/dev/vda ro`, added
   because the drive is `is_root_device`), `init=/init`.
2. Kernel mounts squashfs root RO, execs `/init` (`grl-init`, baked into base).
3. `grl-init`:
   - Mount the ext4 scratch device at `/scratch`; make `/scratch/root/{upper,work}`.
   - Overlay `lowerdir=/ upperdir=/scratch/root/upper workdir=/scratch/root/work`
     → `/newroot`; move `/proc /sys /dev /scratch` in; `pivot_root` → writable `/`.
   - Mount the task squashfs RO at `/mnt/task`; `cp -a /mnt/task/. /testbed/`
     (the copy lands in the overlay upper on scratch → fully writable, all
     renames work); place the answer key where the scorer reads it:
     `mkdir -p /grl && mv /testbed/grl/task.json /grl/task.json`
     (matches `env/src/score.rs` `TASK_SPEC_PATH = "/grl/task.json"`,
     `repo_dir = "/testbed"`).
   - `exec /usr/local/bin/grl-env`.
4. Teardown: `VmHandle::stop` already `rm -rf run_dir`, discarding scratch.

## Component changes

### 1. vms build tooling (`environments/swebench-lite/vms/`)

- `build_images.py`: stop producing padded ext4. Extract the Docker rootfs to a
  directory, add `/init` (grl-init) and `/usr/local/bin/grl-env`, then
  `mksquashfs <rootfs-dir> <name>.squashfs -comp zstd -noappend`. **Delete**
  `_shrink_ext4_bash`, `BUILD_SIZE_MB`, `HEADROOM_MB`, and the
  `dd`/`mkfs`/`mount`/`resize2fs` dance. Ensure the rootfs contains
  `util-linux` (`mount`, `pivot_root`, `blkid`) and coreutils (`cp`) — no
  `e2fsprogs` needed in the guest anymore.
- `build_tasks.py`: emit `<task_id>.squashfs` from the repo tree + `grl/task.json`
  via `mksquashfs`; delete `TASK_BUILD_SIZE_MB`/`TASK_HEADROOM_MB` and the ext4
  path. (`e2fsprogs` no longer needed here.)
- `images.py` (`image_paths`): emit `.squashfs` extensions for `base_image` /
  `task_image`. `NODE_BASES_DIR`/`NODE_TASKS_DIR` unchanged.
- `upload.py`: `_list_uploads` globs `*.squashfs` instead of `*.ext4`.
- The scratch template is **not** a vms artifact and is **not** shipped through
  S3 (a sparse ext4 uploads as full-size zeros). It is created node-locally — see
  Infra.

### 2. Guest init (`environments/swebench-lite/vms/assets/grl-init`)

Rewrite to the overlay/pivot_root/copy-up flow above. Requirements:
- Detect devices via `blkid -o value -s TYPE`: the ext4 device is scratch; the
  squashfs device that is not the mounted root is the task.
- Overlay upper + workdir on the scratch ext4; workdir empty.
- `/testbed` is a fresh copy (upper-only) → directory renames work natively.
- Place `/grl/task.json` for the scorer; keep repo at `/testbed`.
- Final step stays `exec /usr/local/bin/grl-env` (vsock:5005).
- Any failure → nonzero exit → `panic=1` reboot, mirroring current behavior.

### 3. Manager (`environments/manager/`) — always overlay, no legacy branch

**`src/vm/config.rs`**
- `root_drive`: `is_read_only: true`, `is_root_device: true` (base squashfs).
- `task_drive`: `is_read_only: true` (task squashfs).
- New `scratch_drive(paths)` → `{ drive_id:"scratch", path_on_host:
  run_dir/scratch.ext4, is_root_device:false, is_read_only:false }`.
- `boot_args`: include `ro` (squashfs root); keep `init=/init`. Firecracker adds
  `root=/dev/vda` for the root device.

**`src/vm/paths.rs`**
- Add `scratch_path(run_dir)` and `scratch_template_path(cache_root)`
  (`{cache}/scratch-template.ext4`).
- `VmPaths` unchanged; `join_and_verify` already accepts the `.squashfs` relative
  paths from `tasks.jsonl` with no change.

**`src/vm/mod.rs` (`boot`)**
- After `resolve_vm_paths`, copy the template to the per-VM scratch:
  shell `cp --reflink=auto --sparse=always {template} {run_dir}/scratch.ext4`
  (reflink = instant CoW when the cache and run dirs share a CoW fs; otherwise a
  fast sparse copy of the tiny, journal-less template). Requires coreutils `cp`
  in the manager image — verify the Dockerfile (or implement a `FICLONE`/sparse
  copy in Rust to stay dependency-free).
- PUT order: `drives/rootfs` → `drives/task` → `drives/scratch` → `actions`.
  Guest identifies by blkid regardless of enumeration order.
- No mode detection, no `.ext4` fallback — the manager always attaches RO lowers
  + scratch.

**`src/catalog.rs`** — no logic change; update test fixtures to `.squashfs`
(cosmetic; paths are opaque).

### 4. Infra (`infra/modules/resources/chart/templates/`)

- **Scratch template**, staged once per node in `vm-image-cache.yaml`'s
  initContainer (after the image sync), if absent:
  `truncate -s ${SCRATCH_GB}G /data/scratch-template.ext4 &&
   mkfs.ext4 -F -O ^has_journal /data/scratch-template.ext4`.
  `-O ^has_journal` keeps the template's real footprint at a few MB so the
  per-VM sparse copy is fast (scratch is ephemeral — journaling is pointless).
  Add `e2fsprogs` to the vm-image-cache image; expose `SCRATCH_GB` as a Helm
  value (default 20).
- The `bases/*` / `tasks/*` sync globs already match `.squashfs` — no change.
- `manager.yaml`: manager reads the template from the read-only `vm-cache` mount
  and writes scratch under `run_root` (pod-local, writable) — no manifest change
  needed. Confirm coreutils `cp` is present in the manager image.

## Rollout

Greenfield, so no dual-format window:
1. Publish squashfs base/task images + `tasks.jsonl` with `.squashfs` paths for a
   split (e.g. `dev`).
2. Warm the cache (creates the template) and validate end-to-end on a node.
3. Publish remaining splits; delete the old `.ext4` objects from the bucket.

## Edge cases & mitigations

- **`ENOSPC` on scratch**: bounded by `SCRATCH_GB`; size generously (sparse, no
  host cost) and surface disk-full as an env infra error.
- **Boot latency**: template copy is a fast sparse/reflink copy; overlay +
  pivot_root + repo copy-up add ~1 s — negligible vs the 120 s boot budget.
- **Jailer mode** (`GRL_USE_JAILER=1`): drives must live inside the chroot; the
  current code doesn't stage artifacts for jailer, so this is a pre-existing gap,
  not new. Track separately; the default non-jailer path is unaffected.
- **Guest tooling**: base squashfs must include `mount`, `pivot_root`, `blkid`
  (util-linux) and `cp` (coreutils). No `mkfs`/`e2fsprogs` in the guest.

## Testing & verification

1. **Kernel** (done): overlay + squashfs(zstd) + virtio-blk + ext4 confirmed.
2. **Build**: produce one base + one task squashfs; record sizes vs today.
3. **Single-VM boot** on a Linux node: overlay + pivot_root succeed, `/` and
   `/testbed` writable, `/grl/task.json` present, `grl-env` on vsock:5005, a full
   rollout scores correctly.
4. **Directory rename in `/testbed`**: `mv` a repo subdir succeeds (proves
   copy-up avoids `EXDEV`).
5. **Concurrency**: boot N VMs for the same `task_id`; write distinct files in
   each; assert isolation and no backing-file corruption.
6. **Teardown**: scratch removed with `run_dir`.

## Work sequencing

- [x] vms: squashfs build for base + task; delete ext4/shrink/headroom;
      `.squashfs` paths in `tasks.jsonl`; upload globs `.squashfs`.
- [x] guest: new `grl-init` (blkid detect, mount scratch, overlay + pivot_root,
      `/testbed` copy-up, `/grl/task.json` placement).
- [x] infra: `vm-image-cache` creates the journal-less scratch template
      (+`e2fsprogs`, `SCRATCH_GB` Helm value).
- [x] manager: RO lower flags; `scratch_drive` + template copy; `boot_args` `ro`;
      remove any ext4/legacy assumptions; update fixtures.
- [x] tests: manager unit tests (config JSON, scratch copy/path). Guest boot +
      rename + concurrency e2e still pending on a Linux node.
- [ ] roll out `dev` split, measure sizes, publish remaining splits, delete old
      `.ext4` objects.
