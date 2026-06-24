"""Task dataset: the trainer's view of an environment's tasks.jsonl.

The environment publishes its task set as a ``tasks.jsonl`` artifact in S3 (built
and uploaded by ``vms``). The trainer reads only the *index* — which tasks exist
and their split — to enumerate, shuffle, and shard work. It never parses the
task content (prompt, tools): the environment renders those and returns them from
``CreateEnvironment``. This keeps the trainer environment-agnostic.

Set ``dataset.tasks_s3_uri`` in the training config, e.g.
``s3://my-bucket/datasets/swebench-lite/dev/tasks.jsonl``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Task:
    task_id: str
    split: str


def _read_s3_text(uri: str) -> str:
    import boto3

    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"expected an s3://bucket/key URI, got {uri!r}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"]
    return body.read().decode("utf-8")


def parse_tasks(contents: str, *, split: str | None = None) -> list[Task]:
    """Parse tasks.jsonl into the index. Optionally filter to one split."""
    tasks: list[Task] = []
    for line in contents.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        row_split = row.get("split", "")
        if split is not None and row_split != split:
            continue
        tasks.append(Task(task_id=row["task_id"], split=row_split))
    return tasks


def load_tasks(uri: str, *, split: str | None = None) -> list[Task]:
    """Load the task index from the tasks.jsonl object in S3."""
    if not uri:
        raise RuntimeError("dataset.tasks_s3_uri is required")
    return parse_tasks(_read_s3_text(uri), split=split)
