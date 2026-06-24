import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

BASES_PREFIX = "bases"
TASKS_PREFIX = "tasks"
# The trainer dataset (tasks.jsonl) lives under its own prefix, separate from
# the task disk images under TASKS_PREFIX.
DATASETS_PREFIX = "datasets"
DEFAULT_UPLOAD_JOBS = 4
MULTIPART_CHUNK_SIZE = 64 * 1024 * 1024

_thread_local = threading.local()
_print_lock = threading.Lock()
_transfer_config = TransferConfig(
    multipart_threshold=MULTIPART_CHUNK_SIZE,
    multipart_chunksize=MULTIPART_CHUNK_SIZE,
    use_threads=False,
)


@dataclass(frozen=True)
class UploadItem:
    path: Path
    key: str


def s3_config() -> tuple[str, str]:
    bucket = os.environ.get("VMS_S3_BUCKET")
    region = os.environ.get("VMS_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not bucket:
        raise SystemExit("VMS_S3_BUCKET environment variable is required")
    if not region:
        raise SystemExit("VMS_S3_REGION or AWS_DEFAULT_REGION environment variable is required")
    return bucket, region


def default_upload_jobs() -> int:
    raw = os.environ.get("VMS_UPLOAD_JOBS")
    if raw is None:
        return DEFAULT_UPLOAD_JOBS
    try:
        jobs = int(raw)
    except ValueError as exc:
        raise SystemExit("VMS_UPLOAD_JOBS must be an integer") from exc
    if jobs < 1:
        raise SystemExit("VMS_UPLOAD_JOBS must be at least 1")
    return jobs


def _s3_client(region: str):
    client = getattr(_thread_local, "s3", None)
    if client is None:
        client = boto3.client(
            "s3",
            region_name=region,
            config=BotoConfig(
                connect_timeout=30,
                read_timeout=300,
                retries={"max_attempts": 10, "mode": "adaptive"},
                tcp_keepalive=True,
            ),
        )
        _thread_local.s3 = client
    return client


def _log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def _format_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def _object_exists(s3, bucket: str, key: str, size: int) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return head.get("ContentLength") == size


def _list_uploads(local_dir: Path, prefix: str) -> list[UploadItem]:
    if not local_dir.is_dir():
        raise SystemExit(f"directory not found: {local_dir}")

    files = sorted(local_dir.glob("*.ext4"))
    if not files:
        raise SystemExit(f"no .ext4 files found in {local_dir}")

    return [UploadItem(path=path, key=f"{prefix}/{path.name}") for path in files]


def _upload_one(
    item: UploadItem,
    *,
    bucket: str,
    region: str,
    force: bool,
) -> str:
    s3 = _s3_client(region)
    size = item.path.stat().st_size
    uri = f"s3://{bucket}/{item.key}"
    _log(f"check: {uri} ({_format_size(size)})")
    if not force and _object_exists(s3, bucket, item.key, size):
        return "skip"
    _log(f"start: {uri} ({_format_size(size)})")
    s3.upload_file(str(item.path), bucket, item.key, Config=_transfer_config)
    return "uploaded"


def _upload_items(
    items: list[UploadItem],
    *,
    bucket: str,
    region: str,
    force: bool,
    jobs: int,
) -> None:
    if jobs < 1:
        raise SystemExit("--jobs must be at least 1")

    total = len(items)
    errors: list[tuple[UploadItem, BaseException]] = []
    _log(
        f"uploading {total} object(s) to s3://{bucket} with {jobs} job(s) "
        f"and {_format_size(MULTIPART_CHUNK_SIZE)} parts"
    )
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(_upload_one, item, bucket=bucket, region=region, force=force): item
            for item in items
        }
        for i, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            uri = f"s3://{bucket}/{item.key}"
            try:
                status = future.result()
            except Exception as exc:
                errors.append((item, exc))
                _log(f"upload {i}/{total}: {uri} (failed: {exc})")
                continue
            if status == "skip":
                _log(f"upload {i}/{total}: {uri} (skip)")
            else:
                _log(f"upload {i}/{total}: {uri}")

    if errors:
        failed = ", ".join(item.key for item, _ in errors)
        raise SystemExit(f"{len(errors)} upload(s) failed: {failed}")


def upload_dir(
    local_dir: Path,
    prefix: str,
    *,
    force: bool = False,
    jobs: int | None = None,
) -> None:
    bucket, region = s3_config()
    _upload_items(
        _list_uploads(local_dir, prefix),
        bucket=bucket,
        region=region,
        force=force,
        jobs=default_upload_jobs() if jobs is None else jobs,
    )


def upload_tasks_file(path: Path, *, split: str, force: bool = False) -> str:
    """Upload tasks.jsonl to s3://$VMS_S3_BUCKET/datasets/swebench-lite/<split>/tasks.jsonl.

    Returns the s3:// URI, which is what the trainer's GRL_TASKS_S3_URI points at.
    """
    if not path.is_file():
        raise SystemExit(f"tasks file not found: {path}")
    bucket, region = s3_config()
    key = f"{DATASETS_PREFIX}/swebench-lite/{split}/tasks.jsonl"
    s3 = _s3_client(region)
    size = path.stat().st_size
    uri = f"s3://{bucket}/{key}"
    if not force and _object_exists(s3, bucket, key, size):
        _log(f"upload: {uri} (skip)")
        return uri
    _log(f"upload: {uri} ({_format_size(size)})")
    s3.upload_file(str(path), bucket, key, Config=_transfer_config)
    return uri


def upload_all(
    base_dir: Path,
    task_dir: Path,
    *,
    force: bool = False,
    jobs: int | None = None,
) -> None:
    bucket, region = s3_config()
    items = _list_uploads(base_dir, BASES_PREFIX) + _list_uploads(task_dir, TASKS_PREFIX)
    _upload_items(
        items,
        bucket=bucket,
        region=region,
        force=force,
        jobs=default_upload_jobs() if jobs is None else jobs,
    )
