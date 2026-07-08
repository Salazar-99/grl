# Build from the repo root so the proto dir is in the build context:
#   docker build -f environments/manager/Dockerfile -t grl-manager .
FROM rust:1-slim AS build

WORKDIR /build
COPY environments/proto/ environments/proto/
# The env crate is a dev-dependency of the manager; cargo needs its manifest
# to resolve the dependency graph even though it is not compiled here.
COPY environments/swebench-lite/env/ environments/swebench-lite/env/
COPY environments/manager/ environments/manager/

WORKDIR /build/environments/manager
RUN cargo build --release --locked

# Firecracker + jailer: statically linked binaries from the pinned release,
# verified against the published checksum.
FROM debian:bookworm-slim AS firecracker
ARG FIRECRACKER_VERSION=v1.16.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    arch="$(uname -m)"; \
    base="https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}"; \
    curl -fsSLO "${base}/firecracker-${FIRECRACKER_VERSION}-${arch}.tgz"; \
    curl -fsSL "${base}/firecracker-${FIRECRACKER_VERSION}-${arch}.tgz.sha256.txt" | sha256sum -c -; \
    tar -xzf "firecracker-${FIRECRACKER_VERSION}-${arch}.tgz"; \
    install -m 0755 "release-${FIRECRACKER_VERSION}-${arch}/firecracker-${FIRECRACKER_VERSION}-${arch}" /usr/local/bin/firecracker; \
    install -m 0755 "release-${FIRECRACKER_VERSION}-${arch}/jailer-${FIRECRACKER_VERSION}-${arch}" /usr/local/bin/jailer

# Runs as a DaemonSet on every environment node; rollout workers reach it
# through the grl-manager Service. The guest kernel and rootfs images are not
# baked in — the vm-image-cache DaemonSet syncs them from S3 to the node disk,
# mounted at GRL_VM_CACHE_DIR.
FROM debian:bookworm-slim

COPY --from=firecracker /usr/local/bin/firecracker /usr/local/bin/jailer /usr/local/bin/
COPY --from=build /build/environments/manager/target/release/manager /usr/local/bin/manager

ENV GRL_ENV_SERVER_ADDR=0.0.0.0:50051
EXPOSE 50051

ENTRYPOINT ["manager"]
