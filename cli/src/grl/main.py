"""GRL CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from grl import __version__

from grl.config import DEPLOYMENT_TYPES, load_config
from grl.agent import install_skill
from grl.launcher import (cluster_status, create_cluster, launch, list_registered_clusters,
                          list_registered_runs, run_config, run_status, teardown, write_init_config)
from grl.tools import doctor_tools, ensure_tools, list_installed_tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grl", description="GRL cluster launcher")
    parser.add_argument("--version", action="version", version=f"grl {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser("launch", help="Launch a GRL training run")
    launch_parser.add_argument("config", type=Path, help="Path to the run config YAML")
    launch_parser.add_argument(
        "--deployment-type",
        type=str.upper,
        choices=DEPLOYMENT_TYPES,
        help="Override launch.deployment_type (FULL, CLUSTER, RESOURCES, ENVS, or TRAINING)",
    )
    launch_parser.add_argument("--cluster", help="Override infra.cluster_name for this launch")
    launch_parser.add_argument("--env-bundle")
    launch_parser.add_argument("--env-id")
    launch_parser.add_argument("--env-split")
    launch_parser.add_argument("--image-tag")
    for role in ("head", "rollouts", "training", "manager"):
        launch_parser.add_argument(f"--{role}-image", dest=f"{role}_image")
    launch_parser.add_argument("--replace-active", action="store_true")
    launch_parser.add_argument("--wait", action="store_true")
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
    clusters_list = clusters_sub.add_parser("list", help="List known launcher clusters")
    clusters_list.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True)
    clusters_list.add_argument("--output", choices=("table", "json"), default="table")
    clusters_create = clusters_sub.add_parser("create", help="Provision reusable GRL resources")
    clusters_create.add_argument("config", type=Path)
    clusters_create.add_argument("--cluster")
    clusters_create.add_argument("--wait", action="store_true")
    clusters_create.add_argument("--dry-run", action="store_true")
    clusters_status = clusters_sub.add_parser("status", help="Show a registered cluster")
    clusters_status.add_argument("name")
    clusters_status.add_argument("--output", choices=("table", "json"), default="table")

    runs_parser = subparsers.add_parser("runs", help="Locally recorded GRL runs")
    runs_sub = runs_parser.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_sub.add_parser("list")
    runs_list.add_argument("--cluster")
    runs_list.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True)
    runs_list.add_argument("--output", choices=("table", "json"), default="table")
    runs_status = runs_sub.add_parser("status")
    runs_status.add_argument("run_id")
    runs_status.add_argument("--refresh", action="store_true")
    runs_status.add_argument("--output", choices=("table", "json"), default="table")
    runs_config = runs_sub.add_parser("config")
    runs_config.add_argument("run_id")
    runs_config.add_argument("--format", choices=("yaml", "json"), default="yaml")

    agent_parser = subparsers.add_parser("agent", help="Install GRL instructions for an AI agent")
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_setup = agent_sub.add_parser("setup", help="Install the bundled GRL skill")
    agent_target = agent_setup.add_mutually_exclusive_group(required=True)
    agent_target.add_argument("--claude", action="store_true", help="Install to ~/.claude/skills/grl")
    agent_target.add_argument("--codex", action="store_true", help="Install to $CODEX_HOME/skills or ~/.codex/skills")

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
        if args.deployment_type:
            config.launch.deployment_type = args.deployment_type
        if args.cluster:
            config.infra.cluster_name = args.cluster
        for attribute, value in (("bundle_uri", args.env_bundle), ("id", args.env_id), ("split", args.env_split)):
            if value is not None:
                setattr(config.environment, attribute, value)
        if args.image_tag is not None:
            config.images.tag = args.image_tag
        for role in ("head", "rollouts", "training"):
            value = getattr(args, f"{role}_image")
            if value is not None:
                setattr(config.images.training, role, value)
        if args.manager_image is not None:
            config.images.manager = args.manager_image
        if args.replace_active:
            config.launch.job.force = True
        if args.wait:
            config.launch.job.wait = True
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
            if args.output == "json":
                from grl.clusters import list_clusters
                print(json.dumps([item.to_dict() for item in list_clusters()], indent=2))
            else:
                print(list_registered_clusters())
            return 0
        if args.clusters_command == "status":
            try:
                record = cluster_status(args.name)
            except Exception as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(record.to_dict(), indent=2) if args.output == "json" else list_registered_clusters())
            return 0
        if args.clusters_command == "create":
            if not args.config.is_file():
                print(f"error: config file not found: {args.config}", file=sys.stderr)
                return 1
            try:
                config = load_config(args.config)
                if args.cluster:
                    config.infra.cluster_name = args.cluster
                config.launch.dry_run = args.dry_run
                create_cluster(config, wait=args.wait)
            except Exception as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0

    if args.command == "runs":
        if args.runs_command == "list":
            from grl.runs import list_runs
            records = list_runs()
            if args.cluster:
                records = [record for record in records if record.cluster_name == args.cluster]
            print(json.dumps([record.__dict__ for record in records], indent=2) if args.output == "json" else list_registered_runs(cluster_name=args.cluster))
            return 0
        if args.runs_command == "status":
            try:
                record = run_status(args.run_id)
            except Exception as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            from grl.runs import format_table
            print(json.dumps(record.__dict__, indent=2) if args.output == "json" else format_table([record]))
            return 0
        if args.runs_command == "config":
            try:
                print(run_config(args.run_id, format=args.format), end="" if args.format == "yaml" else "\n")
            except Exception as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0

    if args.command == "agent":
        if args.agent_command == "setup":
            agent = "claude" if args.claude else "codex"
            try:
                destination = install_skill(agent)
            except OSError as exc:
                print(f"error: could not install {agent} skill: {exc}", file=sys.stderr)
                return 1
            print(f"Installed GRL skill for {agent} at {destination}")
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
