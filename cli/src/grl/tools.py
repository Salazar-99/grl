"""Managed external tool binaries (Terraform, Helm, kubectl)."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from grl.config import LaunchToolsConfig
from grl.paths import grl_home


class ToolError(Exception):
    """Managed tool installation or execution failed."""


TOOLS_CACHE = Path(os.environ.get("GRL_TOOLS_CACHE", grl_home() / "tools"))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    url: str
    archive_type: str
    binary_path_in_archive: str
    sha256: str | None = None


def platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        machine = "amd64"
    elif machine in {"aarch64", "arm64"}:
        machine = "arm64"
    return system, machine


def terraform_spec(config: LaunchToolsConfig) -> ToolSpec:
    system, machine = platform_key()
    version = config.terraform_version
    if system == "darwin":
        archive = f"terraform_{version}_darwin_{machine}.zip"
        url = f"https://releases.hashicorp.com/terraform/{version}/{archive}"
        return ToolSpec("terraform", version, url, "zip", "terraform")
    if system == "linux":
        archive = f"terraform_{version}_linux_{machine}.zip"
        url = f"https://releases.hashicorp.com/terraform/{version}/{archive}"
        return ToolSpec("terraform", version, url, "zip", "terraform")
    raise ToolError(f"unsupported platform for terraform: {system}/{machine}")


def helm_spec(config: LaunchToolsConfig) -> ToolSpec:
    system, machine = platform_key()
    version = config.helm_version
    if system == "darwin":
        archive = f"helm-v{version}-darwin-{machine}.tar.gz"
        url = f"https://get.helm.sh/{archive}"
        inner = f"darwin-{machine}/helm"
        return ToolSpec("helm", version, url, "tar.gz", inner)
    if system == "linux":
        archive = f"helm-v{version}-linux-{machine}.tar.gz"
        url = f"https://get.helm.sh/{archive}"
        inner = f"linux-{machine}/helm"
        return ToolSpec("helm", version, url, "tar.gz", inner)
    raise ToolError(f"unsupported platform for helm: {system}/{machine}")


def kubectl_spec(config: LaunchToolsConfig) -> ToolSpec:
    system, machine = platform_key()
    version = config.kubectl_version
    if system == "darwin":
        url = f"https://dl.k8s.io/release/v{version}/bin/darwin/{machine}/kubectl"
        return ToolSpec("kubectl", version, url, "binary", "kubectl")
    if system == "linux":
        url = f"https://dl.k8s.io/release/v{version}/bin/linux/{machine}/kubectl"
        return ToolSpec("kubectl", version, url, "binary", "kubectl")
    raise ToolError(f"unsupported platform for kubectl: {system}/{machine}")


def tool_install_path(spec: ToolSpec) -> Path:
    return TOOLS_CACHE / spec.name / spec.version / spec.name


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ToolError(f"checksum mismatch for {path}: expected {expected}, got {digest}")


def extract_tool(spec: ToolSpec, archive_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if spec.archive_type == "binary":
        shutil.copy2(archive_path, destination)
    elif spec.archive_type == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            member = spec.binary_path_in_archive
            with zf.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    elif spec.archive_type == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as tf:
            extracted = tf.extractfile(spec.binary_path_in_archive)
            if extracted is None:
                raise ToolError(f"{spec.binary_path_in_archive} not found in {archive_path}")
            with destination.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)
    else:
        raise ToolError(f"unsupported archive type: {spec.archive_type}")
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_tool(spec: ToolSpec, *, auto_install: bool) -> Path:
    installed = tool_install_path(spec)
    if installed.is_file():
        return installed
    if not auto_install:
        raise ToolError(
            f"{spec.name} {spec.version} is not installed; enable launch.tools.auto_install"
        )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / spec.name
        download_file(spec.url, archive_path)
        if spec.sha256:
            verify_sha256(archive_path, spec.sha256)
        extract_tool(spec, archive_path, installed)
    metadata = {
        "name": spec.name,
        "version": spec.version,
        "url": spec.url,
        "path": str(installed),
    }
    metadata_path = installed.parent / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))
    return installed


def ensure_tools(config: LaunchToolsConfig) -> dict[str, Path]:
    specs = {
        "terraform": terraform_spec(config),
        "helm": helm_spec(config),
        "kubectl": kubectl_spec(config),
    }
    return {
        name: ensure_tool(spec, auto_install=config.auto_install)
        for name, spec in specs.items()
    }


def run_tool(path: Path, args: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a managed tool, streaming its output live.

    Long-running commands (e.g. a ~20 minute ``terraform apply``) echo each
    line as it is produced instead of staying silent until completion. stderr
    is merged into the stream so failures carry the tool's own error text.
    """
    command = [str(path), *args]
    display = f"{path.name} {' '.join(args)}"
    if dry_run:
        print("dry-run:", " ".join(command))
        return subprocess.CompletedProcess(command, 0, "", "")
    print(f"$ {display}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)
        print(f"[{path.name}] {line}", end="", flush=True)
    returncode = process.wait()
    output = "".join(lines)
    if returncode != 0:
        tail = "".join(lines[-20:]).rstrip()
        raise ToolError(
            f"{display} exited with code {returncode}"
            + (f"; last output:\n{tail}" if tail else "")
        )
    return subprocess.CompletedProcess(command, returncode, output, "")


def list_installed_tools() -> list[dict[str, str]]:
    if not TOOLS_CACHE.is_dir():
        return []
    entries: list[dict[str, str]] = []
    for metadata_path in TOOLS_CACHE.glob("*/*/metadata.json"):
        entries.append(json.loads(metadata_path.read_text()))
    return entries


def doctor_tools(config: LaunchToolsConfig) -> dict[str, str | None]:
    report: dict[str, str | None] = {}
    for name, spec in {
        "terraform": terraform_spec(config),
        "helm": helm_spec(config),
        "kubectl": kubectl_spec(config),
    }.items():
        path = tool_install_path(spec)
        report[name] = str(path) if path.is_file() else None
    return report
