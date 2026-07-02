from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3


class CheckpointError(Exception):
    """Checkpoint persistence failed."""


class SavePretrained(Protocol):
    def save_pretrained(self, save_directory: str | Path, **kwargs: Any) -> None: ...


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise CheckpointError(f"unsupported checkpoint bucket URI: {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def checkpoint_prefix(bucket_uri: str, *, run_id: str, policy_version: int) -> tuple[str, str]:
    bucket, prefix = parse_s3_uri(bucket_uri.rstrip("/"))
    checkpoint_key_prefix = "/".join(
        part
        for part in [
            prefix,
            run_id or "default",
            f"step-{policy_version}",
        ]
        if part
    )
    return bucket, checkpoint_key_prefix


def checkpoint_uri(bucket_uri: str, *, run_id: str, policy_version: int) -> str:
    bucket, key_prefix = checkpoint_prefix(
        bucket_uri,
        run_id=run_id,
        policy_version=policy_version,
    )
    return f"s3://{bucket}/{key_prefix}"


def _cpu_state_dict(model: Any) -> dict[str, Any]:
    return {
        name: tensor.detach().to("cpu", copy=True)
        for name, tensor in model.state_dict().items()
    }


def snapshot_checkpoint_dir(
    *,
    model: Any,
    tokenizer: SavePretrained,
    staging_dir: Path | str,
    run_id: str,
    policy_version: int,
) -> Path:
    """Synchronously snapshot live model weights into an immutable local directory."""
    checkpoint_dir = Path(staging_dir) / (run_id or "default") / f"step-{policy_version}"
    if checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True)
    state_dict = _cpu_state_dict(model)
    model.save_pretrained(checkpoint_dir, state_dict=state_dict)
    tokenizer.save_pretrained(checkpoint_dir)
    return checkpoint_dir


def _upload_directory_to_s3(
    directory: Path,
    *,
    bucket: str,
    prefix: str,
) -> None:
    s3 = boto3.client("s3")
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        key = f"{prefix.rstrip('/')}/{relative}" if prefix else relative
        s3.upload_file(str(path), bucket, key)


def upload_checkpoint_dir(
    *,
    checkpoint_dir: Path | str,
    bucket_uri: str,
    run_id: str,
    policy_version: int,
) -> str:
    bucket, key_prefix = checkpoint_prefix(
        bucket_uri,
        run_id=run_id,
        policy_version=policy_version,
    )
    _upload_directory_to_s3(Path(checkpoint_dir), bucket=bucket, prefix=key_prefix)
    return f"s3://{bucket}/{key_prefix}"


def write_checkpoint(
    *,
    model: Any,
    tokenizer: SavePretrained,
    bucket_uri: str,
    run_id: str,
    policy_version: int,
) -> str:
    """Synchronously snapshot and upload a model/tokenizer checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint_dir = snapshot_checkpoint_dir(
            model=model,
            tokenizer=tokenizer,
            staging_dir=tmpdir,
            run_id=run_id,
            policy_version=policy_version,
        )
        return upload_checkpoint_dir(
            checkpoint_dir=checkpoint_dir,
            bucket_uri=bucket_uri,
            run_id=run_id,
            policy_version=policy_version,
        )


class BackgroundCheckpointUploader:
    def __init__(
        self,
        *,
        bucket_uri: str,
        max_background_uploads: int = 1,
    ) -> None:
        self.bucket_uri = bucket_uri
        self.max_background_uploads = max_background_uploads
        self._executor = ThreadPoolExecutor(max_workers=max_background_uploads)
        self._futures: list[Future[str]] = []

    def enqueue(
        self,
        checkpoint_dir: Path,
        *,
        run_id: str,
        policy_version: int,
    ) -> str:
        self.check_completed()
        while len(self._futures) >= self.max_background_uploads:
            self._wait_for_oldest()
        uri = checkpoint_uri(
            self.bucket_uri,
            run_id=run_id,
            policy_version=policy_version,
        )
        future = self._executor.submit(
            self._upload_and_cleanup,
            checkpoint_dir,
            run_id,
            policy_version,
        )
        self._futures.append(future)
        return uri

    def check_completed(self) -> list[str]:
        completed: list[str] = []
        pending: list[Future[str]] = []
        for future in self._futures:
            if future.done():
                completed.append(future.result())
            else:
                pending.append(future)
        self._futures = pending
        return completed

    def wait_all(self) -> list[str]:
        results: list[str] = []
        while self._futures:
            results.append(self._wait_for_oldest())
        return results

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def _wait_for_oldest(self) -> str:
        future = self._futures.pop(0)
        return future.result()

    def _upload_and_cleanup(
        self,
        checkpoint_dir: Path,
        run_id: str,
        policy_version: int,
    ) -> str:
        uri = upload_checkpoint_dir(
            checkpoint_dir=checkpoint_dir,
            bucket_uri=self.bucket_uri,
            run_id=run_id,
            policy_version=policy_version,
        )
        shutil.rmtree(checkpoint_dir, ignore_errors=True)
        return uri
