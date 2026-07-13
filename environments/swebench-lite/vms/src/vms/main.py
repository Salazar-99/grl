import argparse
import json
from pathlib import Path

from vms.build_bootstrap import build_bootstrap
from vms.build_environment import (
    build_environment_image,
    build_minimal_environment_image,
)
from vms.build_images import build_all
from vms.build_tasks import build_all_tasks
from vms.dataset import DEFAULT_DATASET, load_tasks
from vms.dockerfile import render_dockerfile, slug
from vms.images import resolve_from_tasks_jsonl
from vms.kernel_config import validate_kernel_config
from vms.requirements import fetch_requirements
from vms.tasks import write_tasks_jsonl
from vms.upload import (
    upload_all,
    upload_bootstrap,
    upload_environment,
    upload_environment_bundle,
    upload_tasks_file,
)
from vms.versions import MAP_REPO_VERSION_TO_SPECS_PY

ROOT = Path(__file__).resolve().parents[2]


def split_name(dataset: Path) -> str:
    """dev.parquet -> "dev"; the tasks.jsonl split label and S3 path segment."""
    return dataset.stem


def load_envs(tasks: list[dict]) -> dict[tuple[str, str], dict]:
    envs: dict[tuple[str, str], dict] = {}
    for row in tasks:
        key = (row["repo"], row["version"])
        if key in envs:
            continue
        specs = MAP_REPO_VERSION_TO_SPECS_PY[row["repo"]][row["version"]]
        envs[key] = {
            "repo": row["repo"],
            "version": row["version"],
            "specs": specs,
            "env_setup_commit": row["environment_setup_commit"],
        }
    return envs


def generate(tasks: list[dict], output: Path) -> None:
    envs = list(load_envs(tasks).values())
    total = len(envs)
    output.mkdir(parents=True, exist_ok=True)
    for i, env in enumerate(envs, start=1):
        name = slug(env["repo"], env["version"])
        path = output / name / "Dockerfile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_dockerfile(
                env["repo"],
                env["version"],
                env["specs"],
                env["env_setup_commit"],
            )
        )
        if env["specs"].get("packages") == "requirements.txt":
            (path.parent / "requirements.txt").write_text(
                fetch_requirements(env["repo"], env["env_setup_commit"])
            )
        print(f"dockerfile {i}/{total}: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench-lite VM image tooling")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="parquet dataset split (default: data/files/dev.parquet)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate Dockerfiles from dataset")
    gen.add_argument("--output", type=Path, default=ROOT / "dockerfiles")

    tasks_jsonl_cmd = sub.add_parser(
        "tasks", help="render tasks.jsonl (prompts + tools) for the trainer"
    )
    tasks_jsonl_cmd.add_argument("--output", type=Path, default=ROOT / "tasks.jsonl")
    tasks_jsonl_cmd.add_argument(
        "--upload",
        action="store_true",
        help="also upload tasks.jsonl to S3 under datasets/swebench-lite/<split>/",
    )

    lookup = sub.add_parser("resolve", help="look up images for a task id")
    lookup.add_argument("task_id")
    lookup.add_argument(
        "--tasks",
        type=Path,
        default=ROOT / "tasks.jsonl",
        help="tasks.jsonl to read image paths from",
    )

    build = sub.add_parser("build", help="build squashfs firecracker base images")
    build.add_argument("--dockerfiles", type=Path, default=ROOT / "dockerfiles")
    build.add_argument("--output", type=Path, default=ROOT / "base-images")
    build.add_argument("--platform", default="linux/amd64")
    build.add_argument("--only", help="build a single environment by name")
    build.add_argument(
        "--force",
        action="store_true",
        help="rebuild images even if the squashfs output already exists",
    )

    tasks_cmd = sub.add_parser("build-tasks", help="build squashfs task repo images")
    tasks_cmd.add_argument("--output", type=Path, default=ROOT / "task-images")
    tasks_cmd.add_argument("--platform", default="linux/amd64")
    tasks_cmd.add_argument("--only", help="build a single task by instance_id")
    tasks_cmd.add_argument(
        "--force",
        action="store_true",
        help="rebuild images even if the squashfs output already exists",
    )

    upload_bootstrap_cmd = sub.add_parser(
        "upload-bootstrap", help="upload one immutable bootstrap initramfs to S3"
    )
    upload_bootstrap_cmd.add_argument("path", type=Path)
    upload_bootstrap_cmd.add_argument("--force", action="store_true")

    environment_cmd = sub.add_parser(
        "build-environment", help="build the SWE-bench guest runtime squashfs"
    )
    environment_cmd.add_argument(
        "--output", type=Path, default=ROOT / "environment-images"
    )
    environment_cmd.add_argument("--platform", default="linux/amd64")
    environment_cmd.add_argument("--upload", action="store_true")
    environment_cmd.add_argument("--bundle-uri")
    environment_cmd.add_argument("--force", action="store_true")

    minimal_environment_cmd = sub.add_parser(
        "build-minimal-environment",
        help="build the boundary-test managed environment squashfs",
    )
    minimal_environment_cmd.add_argument(
        "--output", type=Path, default=ROOT / "environment-images"
    )
    minimal_environment_cmd.add_argument("--platform", default="linux/amd64")
    minimal_environment_cmd.add_argument("--upload", action="store_true")
    minimal_environment_cmd.add_argument("--force", action="store_true")

    upload_environment_cmd = sub.add_parser(
        "upload-environment", help="upload one environment runtime squashfs"
    )
    upload_environment_cmd.add_argument("path", type=Path)
    upload_environment_cmd.add_argument("--bundle-uri")
    upload_environment_cmd.add_argument("--force", action="store_true")

    bootstrap_cmd = sub.add_parser(
        "build-bootstrap", help="build the final static Rust bootstrap initramfs"
    )
    bootstrap_cmd.add_argument("--output", type=Path, default=ROOT / "bootstrap-images")
    bootstrap_cmd.add_argument("--platform", default="linux/amd64")
    bootstrap_cmd.add_argument("--upload", action="store_true")

    kernel_cmd = sub.add_parser(
        "validate-kernel-config",
        help="verify built-in initrd and mount support in a Linux kernel config",
    )
    kernel_cmd.add_argument("path", type=Path)

    upload_cmd = sub.add_parser("upload", help="upload squashfs images to S3")
    upload_cmd.add_argument("--base-images", type=Path, default=ROOT / "base-images")
    upload_cmd.add_argument("--task-images", type=Path, default=ROOT / "task-images")
    upload_cmd.add_argument(
        "--force",
        action="store_true",
        help="re-upload even if the object already exists with the same size",
    )
    upload_cmd.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="parallel uploads (default: 4 or VMS_UPLOAD_JOBS)",
    )

    args = parser.parse_args()
    tasks = load_tasks(args.dataset)

    split = split_name(args.dataset)

    if args.command == "generate":
        generate(tasks, args.output)
    elif args.command == "tasks":
        write_tasks_jsonl(tasks, split, args.output)
        print(f"wrote {args.output}")
        if args.upload:
            uri = upload_tasks_file(args.output, split=split)
            print(f"uploaded {uri}")
    elif args.command == "resolve":
        row = resolve_from_tasks_jsonl(args.tasks, args.task_id)
        print(json.dumps(row, indent=2))
    elif args.command == "build":
        build_all(
            args.dockerfiles,
            args.output,
            platform=args.platform,
            only=args.only,
            force=args.force,
        )
    elif args.command == "build-tasks":
        build_all_tasks(
            args.dataset,
            args.output,
            platform=args.platform,
            only=args.only,
            force=args.force,
        )
    elif args.command == "upload-bootstrap":
        print(f"uploaded {upload_bootstrap(args.path, force=args.force)}")
    elif args.command == "build-environment":
        artifact = build_environment_image(
            args.output, platform=args.platform, force=args.force
        )
        print(f"wrote {artifact}")
        if args.upload:
            uri = (
                upload_environment_bundle(
                    artifact, bundle_uri=args.bundle_uri, force=args.force
                )
                if args.bundle_uri
                else upload_environment(artifact, force=args.force)
            )
            print(f"uploaded {uri}")
    elif args.command == "upload-environment":
        uri = (
            upload_environment_bundle(
                args.path, bundle_uri=args.bundle_uri, force=args.force
            )
            if args.bundle_uri
            else upload_environment(args.path, force=args.force)
        )
        print(f"uploaded {uri}")
    elif args.command == "build-minimal-environment":
        artifact = build_minimal_environment_image(
            args.output, platform=args.platform, force=args.force
        )
        print(f"wrote {artifact}")
        if args.upload:
            print(f"uploaded {upload_environment(artifact, force=args.force)}")
    elif args.command == "build-bootstrap":
        artifact = build_bootstrap(args.output, platform=args.platform)
        print(f"wrote {artifact}")
        if args.upload:
            print(f"uploaded {upload_bootstrap(artifact)}")
    elif args.command == "validate-kernel-config":
        validate_kernel_config(args.path)
        print(f"kernel config supports managed bootstrap: {args.path}")
    elif args.command == "upload":
        upload_all(args.base_images, args.task_images, force=args.force, jobs=args.jobs)


if __name__ == "__main__":
    main()
