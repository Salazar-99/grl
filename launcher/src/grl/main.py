"""GRL CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from grl.config import load_config
from grl.launcher import launch, list_registered_clusters, teardown, write_init_config
from grl.tools import doctor_tools, ensure_tools, list_installed_tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grl", description="GRL cluster launcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser("launch", help="Launch a GRL training run")
    launch_parser.add_argument("config", type=Path, help="Path to the run config YAML")
    launch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without executing them",
    )
    launch_parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run preflight checks only",
    )

    teardown_parser = subparsers.add_parser(
        "teardown",
        help="Destroy Terraform-managed infrastructure for a cluster",
    )
    teardown_parser.add_argument("config", type=Path, help="Path to the run config YAML")
    teardown_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run terraform plan -destroy without executing destroy",
    )
    teardown_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )

    clusters_parser = subparsers.add_parser("clusters", help="Launcher cluster registry")
    clusters_sub = clusters_parser.add_subparsers(dest="clusters_command", required=True)
    clusters_sub.add_parser("list", help="List known launcher clusters")

    init_parser = subparsers.add_parser("init", help="Write a starter config.yaml")
    init_parser.add_argument(
        "destination",
        type=Path,
        nargs="?",
        default=Path("config.yaml"),
        help="Output path for the starter config",
    )

    tools_parser = subparsers.add_parser("tools", help="Managed external tools")
    tools_sub = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list", help="List installed managed tools")
    tools_sub.add_parser("doctor", help="Report managed tool status")
    install_parser = tools_sub.add_parser("install", help="Install managed tools")
    install_parser.add_argument(
        "config",
        type=Path,
        nargs="?",
        help="Optional config YAML for tool versions",
    )

    args = parser.parse_args(argv)

    if args.command == "launch":
        if not args.config.is_file():
            print(f"error: config file not found: {args.config}", file=sys.stderr)
            return 1
        try:
            config = load_config(args.config)
        except (ValidationError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.dry_run:
            config.launch.dry_run = True
        if args.preflight_only:
            config.launch.preflight_only = True
        try:
            launch(config, config_path=args.config)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "teardown":
        if not args.config.is_file():
            print(f"error: config file not found: {args.config}", file=sys.stderr)
            return 1
        try:
            config = load_config(args.config)
        except (ValidationError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.dry_run:
            config.launch.dry_run = True
        try:
            teardown(config, config_path=args.config, auto_yes=args.yes)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "clusters":
        if args.clusters_command == "list":
            print(list_registered_clusters())
            return 0

    if args.command == "init":
        if args.destination.exists():
            print(f"error: {args.destination} already exists", file=sys.stderr)
            return 1
        write_init_config(args.destination)
        print(f"Wrote starter config to {args.destination}")
        return 0

    if args.command == "tools":
        if args.tools_command == "list":
            tools = list_installed_tools()
            print(json.dumps(tools, indent=2))
            return 0
        if args.tools_command == "doctor":
            from grl.config import GRLConfig

            tool_config = GRLConfig.model_validate({"model": "placeholder"})
            report = doctor_tools(tool_config.launch.tools)
            for name, path in report.items():
                print(f"{name}: {path or 'missing'}")
            return 0
        if args.tools_command == "install":
            if args.config and args.config.is_file():
                tool_config = load_config(args.config).launch.tools
            else:
                from grl.config import LaunchToolsConfig

                tool_config = LaunchToolsConfig()
            paths = ensure_tools(tool_config)
            for name, path in paths.items():
                print(f"installed {name}: {path}")
            return 0

    parser.print_help()
    return 1
