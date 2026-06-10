FROM --platform=linux/amd64 ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.9
RUN uv venv /opt/testbed --python 3.9
ENV PATH=/opt/testbed/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/testbed/bin/python -r /tmp/requirements.txt

WORKDIR /testbed
