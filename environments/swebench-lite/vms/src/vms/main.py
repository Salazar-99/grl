import argparse
import json
from pathlib import Path

from vms.build_images import build_all
from vms.build_tasks import build_all_tasks
from vms.dataset import DEFAULT_DATASET, load_tasks
from vms.dockerfile import render_dockerfile, slug
from vms.images import resolve_from_tasks_jsonl
from vms.requirements import fetch_requirements
from vms.tasks import write_tasks_jsonl
from vms.upload import upload_all, upload_tasks_file
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
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild images even if the squashfs output already exists",
    )
    parser.add_argument(
        "--upload-jobs",
        type=int,
        default=None,
        help="parallel uploads for the full pipeline (default: 4 or VMS_UPLOAD_JOBS)",
    )
    sub = parser.add_subparsers(dest="command")

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

    dockerfiles = ROOT / "dockerfiles"
    base_images = ROOT / "base-images"
    task_images = ROOT / "task-images"

    tasks_jsonl = ROOT / "tasks.jsonl"
    split = split_name(args.dataset)

    if args.command is None:
        generate(tasks, dockerfiles)
        build_all(dockerfiles, base_images, platform=args.platform, force=args.force)
        build_all_tasks(
            args.dataset, task_images, platform=args.platform, force=args.force
        )
        write_tasks_jsonl(tasks, split, tasks_jsonl)
        upload_all(base_images, task_images, force=args.force, jobs=args.upload_jobs)
        upload_tasks_file(tasks_jsonl, split=split, force=args.force)
    elif args.command == "generate":
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
    elif args.command == "upload":
        upload_all(args.base_images, args.task_images, force=args.force, jobs=args.jobs)


if __name__ == "__main__":
    main()
