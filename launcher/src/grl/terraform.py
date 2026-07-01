"""Terraform infrastructure provisioning helpers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from grl.config import GRLConfig, ResolvedImages
from grl.errors import TerraformError
from grl.paths import state_dir, terraform_dir
from grl.tools import run_tool


def write_tfvars(config: GRLConfig, resolved: ResolvedImages, run_id: str) -> Path:
    vars_path = state_dir(run_id) / "terraform.auto.tfvars.json"
    payload = config.terraform_vars(resolved)
    vars_path.write_text(json.dumps(payload, indent=2))
    return vars_path


def write_helm_overlay(config: GRLConfig, run_id: str) -> Path:
    overlay_path = state_dir(run_id) / "helm-overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(config.helm_values_overlay(), sort_keys=False))
    return overlay_path


def terraform_init(terraform_bin: Path, tf_root: Path, *, dry_run: bool = False) -> None:
    run_tool(terraform_bin, ["init", "-input=false"], cwd=tf_root, dry_run=dry_run)


def terraform_plan(
    terraform_bin: Path,
    tf_root: Path,
    tfvars: Path,
    *,
    dry_run: bool = False,
) -> None:
    run_tool(
        terraform_bin,
        ["plan", "-input=false", f"-var-file={tfvars}"],
        cwd=tf_root,
        dry_run=dry_run,
    )


def terraform_apply(
    terraform_bin: Path,
    tf_root: Path,
    tfvars: Path,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        terraform_plan(terraform_bin, tf_root, tfvars, dry_run=True)
        return
    try:
        run_tool(
            terraform_bin,
            ["apply", "-input=false", "-auto-approve", f"-var-file={tfvars}"],
            cwd=tf_root,
            dry_run=False,
        )
    except Exception as exc:
        raise TerraformError(f"terraform apply failed: {exc}") from exc


def apply_infra(
    config: GRLConfig,
    resolved: ResolvedImages,
    terraform_bin: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    tf_root = terraform_dir(config.launch.infra.terraform_dir)
    tfvars = write_tfvars(config, resolved, run_id)
    terraform_init(terraform_bin, tf_root, dry_run=dry_run)
    terraform_apply(terraform_bin, tf_root, tfvars, dry_run=dry_run)
    return tfvars
