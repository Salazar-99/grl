ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app/training

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/training/.venv/bin:${PATH}"

COPY training/pyproject.toml training/uv.lock training/README.md ./
COPY config /app/config
COPY proto /app/proto
COPY training/src/ ./src/

FROM base AS head
RUN uv sync --no-dev --locked --no-cache
CMD ["training"]

FROM base AS training
RUN uv sync --extra training --no-dev --locked --no-cache

FROM base AS rollouts
# Triton + FlashInfer JIT-compile CUDA helpers at vLLM startup (e.g. Qwen3.5
# multimodal profiling / sampling kernels). python:slim has no host compiler,
# and FlashInfer looks for nvcc via PATH or CUDA_HOME=/usr/local/cuda.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --extra rollouts --no-dev --locked --no-cache
# Compiler stack is pinned to CUDA 13.0 via tool.uv.override-dependencies
# (matches torch cu130 runtime headers for FlashInfer JIT).
# Expose the pip-shipped toolkit where FlashInfer expects it.
RUN python - <<'PY'
from pathlib import Path
import sysconfig

nvidia = Path(sysconfig.get_path("purelib")) / "nvidia"
nvccs = sorted(nvidia.glob("*/bin/nvcc"))
if not nvccs:
    raise SystemExit(f"nvcc not found under {nvidia}")
cu_root = nvccs[0].parent.parent
lib = cu_root / "lib"
lib64 = cu_root / "lib64"
if lib.is_dir() and not lib64.exists():
    lib64.symlink_to(lib)
cudart = next(lib.glob("libcudart.so.*"), None) if lib.is_dir() else None
if cudart is not None:
    unversioned = lib64 / "libcudart.so"
    if not unversioned.exists():
        unversioned.symlink_to(cudart.name)
dst = Path("/usr/local/cuda")
if dst.exists() or dst.is_symlink():
    dst.unlink()
dst.symlink_to(cu_root)
print(f"CUDA_HOME -> {cu_root}")
PY
ENV CUDA_HOME=/usr/local/cuda \
    PATH="/usr/local/cuda/bin:${PATH}" \
    LIBRARY_PATH="/usr/local/cuda/lib64:${LIBRARY_PATH}" \
    LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH}"
