"""S3 bundle preflight checks."""

from __future__ import annotations

from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from grl.config import GRLConfig


class PreflightError(Exception):
    """Preflight validation failed."""


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise PreflightError(f"invalid S3 URI: {uri!r}")
    key = parsed.path.lstrip("/")
    return parsed.netloc, key


def head_object_exists(bucket: str, key: str, *, region: str | None = None) -> bool:
    client = boto3.client("s3", region_name=region)
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise PreflightError(f"S3 head_object failed for s3://{bucket}/{key}: {exc}") from exc


def verify_bundle(config: GRLConfig) -> None:
    env = config.environment
    if not env.bundle_uri:
        raise PreflightError("environment.bundle_uri is required for launch")
    bucket, prefix = parse_s3_uri(env.bundle_uri.rstrip("/"))
    region = config.infra.vm_image_cache.region or config.infra.region
    tasks_key = f"{prefix}/tasks.jsonl" if prefix else "tasks.jsonl"
    if not head_object_exists(bucket, tasks_key, region=region):
        raise PreflightError(f"missing bundle artifact s3://{bucket}/{tasks_key}")
