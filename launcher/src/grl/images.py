"""Resolve runtime container image references."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from grl.config import GRLConfig, ResolvedImages


class GrlError(Exception):
    """Base error for GRL launcher failures."""
from grl.paths import repo_root


@dataclass
class BuildPlan:
    commands: list[list[str]]
    pushed_images: ResolvedImages


def published_image_name(registry: str, component: str, tag: str) -> str:
    registry = registry.rstrip("/")
    return f"{registry}-training-{component}:{tag}" if component != "manager" else f"{registry}-manager:{tag}"


def resolve_published(config: GRLConfig) -> ResolvedImages:
    images = config.images
    registry = images.registry.rstrip("/")
    tag = images.tag

    def resolve_one(value: str, component: str) -> str:
        if value != "auto":
            return value
        if component == "manager":
            return f"{registry}-manager:{tag}"
        return f"{registry}-training-{component}:{tag}"

    return ResolvedImages(
        head=resolve_one(images.training.head, "head"),
        rollouts=resolve_one(images.training.rollouts, "rollouts"),
        training=resolve_one(images.training.training, "training"),
        manager=resolve_one(images.manager, "manager"),
    )


def resolve_custom(config: GRLConfig) -> ResolvedImages:
    images = config.images
    return ResolvedImages(
        head=images.training.head,
        rollouts=images.training.rollouts,
        training=images.training.training,
        manager=images.manager,
    )


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "version"],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def build_context_dir(config: GRLConfig) -> Path:
    build = config.images.build
    if build.source == "path":
        if not build.path:
            raise GrlError("images.build.path is required when source is path")
        return Path(build.path).resolve()
    if build.source == "checkout":
        root = repo_root()
        if root is None:
            raise GrlError(
                "images.mode build_and_push with source checkout requires a GRL repo checkout"
            )
        return root
    raise GrlError(f"unsupported build source: {build.source!r}")


def plan_build_and_push(config: GRLConfig, tag: str) -> BuildPlan:
    context = build_context_dir(config)
    registry = config.images.registry.rstrip("/")
    commands: list[list[str]] = []

    training_dockerfile = context / "training" / "Dockerfile"
    manager_dockerfile = context / "environments" / "manager" / "Dockerfile"

    targets = [
        ("head", "head"),
        ("rollouts", "rollouts"),
        ("training", "training"),
    ]
    resolved_training: dict[str, str] = {}
    for component, target in targets:
        image_ref = f"{registry}-training-{component}:{tag}"
        resolved_training[component] = image_ref
        # Context is the repo root: the training Dockerfile copies the
        # sibling config/ and proto/ directories.
        commands.append(
            [
                "docker",
                "build",
                "--target",
                target,
                "-t",
                image_ref,
                "-f",
                str(training_dockerfile),
                str(context),
            ]
        )
        commands.append(["docker", "push", image_ref])

    manager_ref = f"{registry}-manager:{tag}"
    commands.append(
        [
            "docker",
            "build",
            "-t",
            manager_ref,
            "-f",
            str(manager_dockerfile),
            str(context),
        ]
    )
    commands.append(["docker", "push", manager_ref])

    return BuildPlan(
        commands=commands,
        pushed_images=ResolvedImages(
            head=resolved_training["head"],
            rollouts=resolved_training["rollouts"],
            training=resolved_training["training"],
            manager=manager_ref,
        ),
    )


def resolve_runtime_images(config: GRLConfig, *, dry_run: bool = False) -> ResolvedImages:
    mode = config.images.mode
    if mode == "published":
        return resolve_published(config)
    if mode == "custom":
        return resolve_custom(config)
    if mode == "build_and_push":
        if dry_run:
            tag = config.images.tag
            registry = config.images.registry.rstrip("/")
            return ResolvedImages(
                head=f"{registry}-training-head:{tag}",
                rollouts=f"{registry}-training-rollouts:{tag}",
                training=f"{registry}-training-training:{tag}",
                manager=f"{registry}-manager:{tag}",
            )
        if not docker_available():
            raise GrlError("docker is required for images.mode build_and_push")
        plan = plan_build_and_push(config, config.images.tag)
        for command in plan.commands:
            subprocess.run(command, check=True)
        return plan.pushed_images
    raise GrlError(f"unsupported images.mode: {mode!r}")
