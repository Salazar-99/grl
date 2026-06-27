"""GRL launcher CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grl.config import GRLConfig, load_config


def _launch(config_path: Path) -> int:
    config = load_config(config_path)
    _print_config_summary(config)
    return 0


def _print_config_summary(config: GRLConfig) -> None:
    manager = config.resolved_manager()
    print(f"Loaded config for run {config.resolve_run_id()}")
    print(f"  model: {config.model} (local: {config.resolved_model_path()})")
    print(f"  environment: id={config.environment.id!r} split={config.environment.split!r}")
    print(f"  manager: bundle_uri={manager.bundle_uri!r} env_id={manager.env_id!r}")
    model_cache = config.infra.model_cache
    if model_cache.tag:
        token_status = "set" if model_cache.huggingface_token else "unset"
        print(
            f"  model cache: tag={model_cache.tag!r} "
            f"revision={model_cache.revision!r} hf_token={token_status}"
        )
    print(f"  ray address: {config.infra.ray_address}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grl", description="GRL cluster launcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser(
        "launch",
        help="Load and validate a run config YAML",
    )
    launch_parser.add_argument(
        "config",
        type=Path,
        help="Path to the run config YAML file",
    )

    args = parser.parse_args(argv)

    if args.command == "launch":
        if not args.config.is_file():
            print(f"error: config file not found: {args.config}", file=sys.stderr)
            return 1
        return _launch(args.config)

    parser.print_help()
    return 1
