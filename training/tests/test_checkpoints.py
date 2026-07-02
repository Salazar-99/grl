from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from training.checkpoints import (
    BackgroundCheckpointUploader,
    CheckpointError,
    parse_s3_uri,
    snapshot_checkpoint_dir,
    upload_checkpoint_dir,
    write_checkpoint,
)


class FakeTensor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, object]] = []

    def detach(self) -> "FakeTensor":
        self.calls.append(("detach", None))
        return self

    def to(self, device: str, *, copy: bool = False) -> str:
        self.calls.append(("to", (device, copy)))
        return f"{self.name}-{device}-copy-{copy}"


class Saveable:
    def __init__(self, filename: str, state: dict[str, FakeTensor] | None = None) -> None:
        self.filename = filename
        self.state = state or {}
        self.saved_state_dict: dict[str, object] | None = None

    def state_dict(self) -> dict[str, FakeTensor]:
        return self.state

    def save_pretrained(self, path: str | Path, **kwargs: object) -> None:
        self.saved_state_dict = kwargs.get("state_dict")  # type: ignore[assignment]
        Path(path, self.filename).write_text(self.filename)


def test_parse_s3_uri():
    assert parse_s3_uri("s3://bucket/prefix") == ("bucket", "prefix")
    assert parse_s3_uri("s3://bucket") == ("bucket", "")


def test_parse_s3_uri_rejects_non_s3():
    with pytest.raises(CheckpointError, match="unsupported"):
        parse_s3_uri("gs://bucket/prefix")


def test_snapshot_checkpoint_dir_uses_cpu_state_dict(tmp_path):
    weight = FakeTensor("weight")
    model = Saveable("model.bin", state={"weight": weight})
    tokenizer = Saveable("tokenizer.json")

    checkpoint_dir = snapshot_checkpoint_dir(
        model=model,
        tokenizer=tokenizer,
        staging_dir=tmp_path,
        run_id="grl-test",
        policy_version=7,
    )

    assert checkpoint_dir == tmp_path / "grl-test" / "step-7"
    assert model.saved_state_dict == {"weight": "weight-cpu-copy-True"}
    assert weight.calls == [("detach", None), ("to", ("cpu", True))]
    assert (checkpoint_dir / "model.bin").is_file()
    assert (checkpoint_dir / "tokenizer.json").is_file()


def test_upload_checkpoint_dir_uploads_model_and_tokenizer_files(tmp_path):
    uploads: list[tuple[str, str, str]] = []
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    (checkpoint_dir / "model.bin").write_text("model")
    (checkpoint_dir / "tokenizer.json").write_text("tokenizer")

    class FakeS3:
        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploads.append((Path(filename).name, bucket, key))

    with patch("training.checkpoints.boto3.client", return_value=FakeS3()):
        uri = upload_checkpoint_dir(
            checkpoint_dir=checkpoint_dir,
            bucket_uri="s3://bucket/checkpoints",
            run_id="grl-test",
            policy_version=7,
        )

    assert uri == "s3://bucket/checkpoints/grl-test/step-7"
    assert uploads == [
        ("model.bin", "bucket", "checkpoints/grl-test/step-7/model.bin"),
        ("tokenizer.json", "bucket", "checkpoints/grl-test/step-7/tokenizer.json"),
    ]


def test_write_checkpoint_snapshots_and_uploads(tmp_path):
    uploads: list[tuple[str, str, str]] = []

    class FakeS3:
        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploads.append((Path(filename).name, bucket, key))

    with patch("training.checkpoints.boto3.client", return_value=FakeS3()):
        uri = write_checkpoint(
            model=Saveable("model.bin", state={"weight": FakeTensor("weight")}),
            tokenizer=Saveable("tokenizer.json"),
            bucket_uri="s3://bucket/checkpoints",
            run_id="grl-test",
            policy_version=8,
        )

    assert uri == "s3://bucket/checkpoints/grl-test/step-8"
    assert uploads == [
        ("model.bin", "bucket", "checkpoints/grl-test/step-8/model.bin"),
        ("tokenizer.json", "bucket", "checkpoints/grl-test/step-8/tokenizer.json"),
    ]


def test_background_uploader_uploads_and_cleans_checkpoint_dir(tmp_path):
    uploads: list[tuple[str, str, str]] = []
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    (checkpoint_dir / "model.bin").write_text("model")

    class FakeS3:
        def upload_file(self, filename: str, bucket: str, key: str) -> None:
            uploads.append((Path(filename).name, bucket, key))

    with patch("training.checkpoints.boto3.client", return_value=FakeS3()):
        uploader = BackgroundCheckpointUploader(
            bucket_uri="s3://bucket/checkpoints",
            max_background_uploads=1,
        )
        uri = uploader.enqueue(
            checkpoint_dir,
            run_id="grl-test",
            policy_version=9,
        )
        results = uploader.wait_all()
        uploader.shutdown()

    assert uri == "s3://bucket/checkpoints/grl-test/step-9"
    assert results == [uri]
    assert uploads == [("model.bin", "bucket", "checkpoints/grl-test/step-9/model.bin")]
    assert not checkpoint_dir.exists()
