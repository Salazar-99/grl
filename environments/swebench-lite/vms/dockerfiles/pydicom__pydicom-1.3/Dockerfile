FROM --platform=linux/amd64 ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-py311_23.11.0-2-Linux-x86_64.sh -o /tmp/miniconda.sh
RUN bash /tmp/miniconda.sh -b -p /opt/miniconda3 && rm /tmp/miniconda.sh
RUN /opt/miniconda3/bin/conda create -p /opt/testbed python=3.6 -y
ENV PATH=/opt/testbed/bin:$PATH
RUN uv pip install --python /opt/testbed/bin/python numpy pytest

WORKDIR /testbed
