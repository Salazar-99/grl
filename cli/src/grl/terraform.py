"""Terraform infrastructure provisioning helpers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from grl.config import GRLConfig, ResolvedImages
from grl.paths import (
    byok_terraform_dir,
    state_dir,
    terraform_dir,
    terraform_state_path,
)
from grl.tools import run_tool


class TerraformError(Exception):
    """Terraform operation failed."""


def write_tfvars(
    config: GRLConfig,
    resolved: ResolvedImages,
    run_id: str,
    *,
    byok: bool = False,
    for_teardown: bool = False,
) -> Path:
    filename = "byok.auto.tfvars.json" if byok else "terraform.auto.tfvars.json"
    vars_path = state_dir(run_id) / filename
    if byok:
        payload = (
            config.byok_terraform_vars_for_teardown(resolved)
            if for_teardown
            else config.byok_terraform_vars(resolved)
        )
    else:
        payload = (
            config.terraform_vars_for_teardown(resolved)
            if for_teardown
            else config.terraform_vars(resolved)
        )
    vars_path.write_text(json.dumps(payload, indent=2))
    return vars_path


def write_helm_overlay(config: GRLConfig, run_id: str) -> Path:
    overlay_path = state_dir(run_id) / "helm-overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(config.helm_values_overlay(), sort_keys=False))
    return overlay_path


def write_env_overlay(config: GRLConfig, run_id: str) -> Path:
    """Write the values overlay for the launcher-owned ``environments`` chart."""
    overlay_path = state_dir(run_id) / "env-overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(config.env_helm_values(), sort_keys=False))
    return overlay_path


def _state_args(state_path: Path) -> list[str]:
    return [f"-state={state_path}", f"-backup={state_path}.backup"]


def _prepare_state(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)


def terraform_init(terraform_bin: Path, tf_root: Path, *, dry_run: bool = False) -> None:
    run_tool(terraform_bin, ["init", "-input=false"], cwd=tf_root, dry_run=dry_run)


def terraform_plan(
    terraform_bin: Path,
    tf_root: Path,
    tfvars: Path,
    state_path: Path,
    *,
    destroy: bool = False,
    dry_run: bool = False,
) -> None:
    args = ["plan", "-input=false", *_state_args(state_path), f"-var-file={tfvars}"]
    if destroy:
        args.append("-destroy")
    run_tool(terraform_bin, args, cwd=tf_root, dry_run=dry_run)


def terraform_apply(
    terraform_bin: Path,
    tf_root: Path,
    tfvars: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        terraform_plan(terraform_bin, tf_root, tfvars, state_path, dry_run=True)
        return
    try:
        run_tool(
            terraform_bin,
            [
                "apply",
                "-input=false",
                "-auto-approve",
                *_state_args(state_path),
                f"-var-file={tfvars}",
            ],
            cwd=tf_root,
            dry_run=False,
        )
    except Exception as exc:
        raise TerraformError(f"terraform apply failed: {exc}") from exc


def terraform_destroy(
    terraform_bin: Path,
    tf_root: Path,
    tfvars: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        terraform_plan(
            terraform_bin,
            tf_root,
            tfvars,
            state_path,
            destroy=True,
            dry_run=True,
        )
        return
    if not state_path.is_file():
        raise TerraformError(
            f"no Terraform state found for cluster at {state_path}; nothing to destroy"
        )
    try:
        run_tool(
            terraform_bin,
            [
                "destroy",
                "-input=false",
                "-auto-approve",
                *_state_args(state_path),
                f"-var-file={tfvars}",
            ],
            cwd=tf_root,
            dry_run=False,
        )
    except Exception as exc:
        raise TerraformError(f"terraform destroy failed: {exc}") from exc


def _apply_terraform_root(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    tf_root: Path,
    *,
    byok: bool = False,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    state_path = terraform_state_path(config.infra.cluster_name, byok=byok)
    _prepare_state(state_path)
    print(f"Terraform state: {state_path}")
    tfvars = write_tfvars(config, resolved, run_id, byok=byok)
    terraform_init(terraform_bin, tf_root, dry_run=dry_run)
    terraform_apply(terraform_bin, tf_root, tfvars, state_path, dry_run=dry_run)
    return tfvars, state_path


def _destroy_terraform_root(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    tf_root: Path,
    *,
    byok: bool = False,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    state_path = terraform_state_path(config.infra.cluster_name, byok=byok)
    _prepare_state(state_path)
    print(f"Terraform state: {state_path}")
    tfvars = write_tfvars(config, resolved, run_id, byok=byok, for_teardown=True)
    terraform_init(terraform_bin, tf_root, dry_run=dry_run)
    terraform_destroy(terraform_bin, tf_root, tfvars, state_path, dry_run=dry_run)
    return tfvars, state_path


def apply_full_stack_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    tf_root = terraform_dir(config.launch.infra.terraform_dir)
    tfvars, _ = _apply_terraform_root(
        config,
        resolved,
        terraform_bin,
        run_id,
        tf_root,
        byok=False,
        dry_run=dry_run,
    )
    return tfvars


def apply_byok_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    if not config.launch.infra.kubeconfig:
        raise TerraformError(
            "launch.infra.kubeconfig is required to apply BYOK Terraform"
        )
    tf_root = byok_terraform_dir(config.launch.infra.byok_terraform_dir)
    tfvars, _ = _apply_terraform_root(
        config,
        resolved,
        terraform_bin,
        run_id,
        tf_root,
        byok=True,
        dry_run=dry_run,
    )
    return tfvars


def destroy_full_stack_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    tf_root = terraform_dir(config.launch.infra.terraform_dir)
    tfvars, _ = _destroy_terraform_root(
        config,
        resolved,
        terraform_bin,
        run_id,
        tf_root,
        byok=False,
        dry_run=dry_run,
    )
    return tfvars


def destroy_byok_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    if not config.launch.infra.kubeconfig:
        raise TerraformError(
            "launch.infra.kubeconfig is required to destroy BYOK Terraform"
        )
    tf_root = byok_terraform_dir(config.launch.infra.byok_terraform_dir)
    tfvars, _ = _destroy_terraform_root(
        config,
        resolved,
        terraform_bin,
        run_id,
        tf_root,
        byok=True,
        dry_run=dry_run,
    )
    return tfvars


def apply_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path | None:
    """Apply Terraform for the CLUSTER/RESOURCES layers, routed by cluster_type.

    BYOK targets a pre-existing cluster, so only its RESOURCES layer runs
    Terraform (the ``infra/byok`` root). EKS runs the ``infra/aws`` root whenever
    CLUSTER or RESOURCES is in play; ``deploy_workloads`` (via terraform_vars)
    gates the charts + resources modules so CLUSTER alone provisions only the
    VPC + EKS cluster.
    """
    launch = config.launch

    if launch.is_byok():
        if launch.runs_resources():
            return apply_byok_infra(
                config,
                resolved,
                terraform_bin,
                run_id,
                dry_run=dry_run,
            )
        return None

    if launch.runs_cluster() or launch.runs_resources():
        return apply_full_stack_infra(
            config,
            resolved,
            terraform_bin,
            run_id,
            dry_run=dry_run,
        )

    return None


def destroy_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    """Destroy Terraform-managed infrastructure for the configured cluster."""
    if config.launch.is_byok():
        return destroy_byok_infra(
            config,
            resolved,
            terraform_bin,
            run_id,
            dry_run=dry_run,
        )
    return destroy_full_stack_infra(
        config,
        resolved,
        terraform_bin,
        run_id,
        dry_run=dry_run,
    )
