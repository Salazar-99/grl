import shlex

from vms.env_yml import fetch_env_yml, packages_from_env_yml

VENV = "/opt/testbed"
MINICONDA = (
    "https://repo.anaconda.com/miniconda/Miniconda3-py311_23.11.0-2-Linux-x86_64.sh"
)
LEGACY_PYTHONS = {"3.5", "3.6"}

UV_BASE = f"""\
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/python
"""


def slug(repo: str, version: str) -> str:
    return f"{repo.replace('/', '__')}-{version}"


def _python_setup(python: str) -> list[str]:
    if python in LEGACY_PYTHONS:
        return [
            f"RUN curl -fsSL {MINICONDA} -o /tmp/miniconda.sh",
            "RUN bash /tmp/miniconda.sh -b -p /opt/miniconda3 && rm /tmp/miniconda.sh",
            f"RUN /opt/miniconda3/bin/conda create -p {VENV} python={python} -y",
        ]
    return [
        f"RUN uv python install {python}",
        f"RUN uv venv {VENV} --python {python}",
    ]


def _pip_install(packages: list[str]) -> str:
    pkgs = " ".join(shlex.quote(p) for p in packages)
    return f"RUN uv pip install --python {VENV}/bin/python {pkgs}"


def _packages(specs: dict, repo: str, env_setup_commit: str) -> list[str]:
    packages = specs.get("packages", "")
    if packages == "requirements.txt":
        return []  # handled separately via requirements file
    if packages == "environment.yml":
        return packages_from_env_yml(fetch_env_yml(repo, env_setup_commit))
    if packages:
        return shlex.split(packages)
    return []


def render_dockerfile(
    repo: str, version: str, specs: dict, env_setup_commit: str
) -> str:
    python = specs["python"]
    packages = specs.get("packages", "")

    lines = [
        "FROM --platform=linux/amd64 ubuntu:22.04",
        "",
        "ARG DEBIAN_FRONTEND=noninteractive",
        "ENV TZ=Etc/UTC",
        "",
        UV_BASE.rstrip(),
        *_python_setup(python),
        f"ENV PATH={VENV}/bin:$PATH",
    ]

    if packages == "requirements.txt":
        lines.append("COPY requirements.txt /tmp/requirements.txt")
        lines.append(_pip_install(["-r", "/tmp/requirements.txt"]))
        if pip_packages := specs.get("pip_packages"):
            lines.append(_pip_install(pip_packages))
    else:
        to_install = _packages(specs, repo, env_setup_commit)
        if pip_packages := specs.get("pip_packages"):
            to_install = to_install + pip_packages
        if to_install:
            lines.append(_pip_install(to_install))

    lines.extend(["", "WORKDIR /testbed", ""])
    return "\n".join(lines)
