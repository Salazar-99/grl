ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

FROM base AS head
RUN uv sync --extra environment --no-dev --locked --no-cache
CMD ["training"]

FROM base AS environment
RUN uv sync --extra environment --no-dev --locked --no-cache

FROM base AS training
RUN uv sync --extra training --no-dev --locked --no-cache

FROM base AS rollouts
RUN uv sync --extra rollouts --no-dev --locked --no-cache
