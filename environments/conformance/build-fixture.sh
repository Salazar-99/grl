#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="${GRL_CONFORMANCE_ROOT:-$repo_root/environments/conformance/out}"
kernel="${GRL_CONFORMANCE_KERNEL:?set GRL_CONFORMANCE_KERNEL to an uncompressed x86_64 vmlinux}"

rm -rf "$output"
mkdir -p "$output"/{active,bootstrap,images,kernel,run}
cp "$kernel" "$output/kernel/vmlinux"

tools="$repo_root/environments/swebench-lite/vms"
(cd "$tools" && uv run vms build-bootstrap --output "$output/bootstrap-build")
(cd "$tools" && uv run vms build-minimal-environment --output "$output/environment-build" --force)
cp "$(ls "$output"/bootstrap-build/grl-bootstrap-*.cpio.gz)" "$output/bootstrap/active.cpio.gz"
cp "$(ls "$output"/environment-build/minimal-*.squashfs)" "$output/active/environment.squashfs"

fixture_root="$(mktemp -d)"
trap 'rm -rf "$fixture_root"' EXIT
mkdir -p "$fixture_root/base" "$fixture_root/task"
printf 'grl-kvm-conformance\n' > "$fixture_root/task/fixture"
docker run --rm --platform linux/amd64 \
  -v "$fixture_root:/fixture:ro" -v "$output/images:/output" ubuntu:22.04 \
  bash -ceu '
    apt-get update -qq
    apt-get install -y -qq squashfs-tools e2fsprogs
    mksquashfs /fixture/base /output/base.squashfs -noappend -all-root -all-time 0 -mkfs-time 0 >/dev/null
    mksquashfs /fixture/task /output/task.squashfs -noappend -all-root -all-time 0 -mkfs-time 0 >/dev/null
    truncate -s 256M /output/scratch-template.ext4
    mkfs.ext4 -F -O ^has_journal /output/scratch-template.ext4 >/dev/null
  '
mv "$output/images/scratch-template.ext4" "$output/scratch-template.ext4"
printf 'fixture ready at %s\n' "$output"
