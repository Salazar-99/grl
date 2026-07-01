"""Ensure checked-in Python proto stubs match the canonical .proto file."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB_DIR = REPO_ROOT / "proto/src/grl_proto/grl/environment/v1"
TRACKED = (
    STUB_DIR / "environment_pb2.py",
    STUB_DIR / "environment_pb2_grpc.py",
    STUB_DIR / "environment_pb2.pyi",
)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_generated_proto_matches_canonical_source():
    before = {path: _checksum(path) for path in TRACKED}
    result = subprocess.run(
        [sys.executable, "-m", "grl_proto.generate_proto"],
        cwd=REPO_ROOT / "proto",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    after = {path: _checksum(path) for path in TRACKED}
    assert before == after, "regenerate proto stubs and commit the updated files"
