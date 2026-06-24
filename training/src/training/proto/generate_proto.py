"""Generate Python gRPC stubs from the shared proto definitions."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PYTHON_OUT = Path(__file__).resolve().parent
TRAINING_ROOT = PYTHON_OUT.parents[2]
REPO_ROOT = TRAINING_ROOT.parent
PROTO_ROOT = REPO_ROOT / "environments/proto"
PROTO_FILE = PROTO_ROOT / "grl/environment/v1/environment.proto"
GRPC_IMPORT_PREFIX = "from training.proto.grl.environment.v1 import environment_pb2"


def _ensure_package(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    init_file = path / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""Generated gRPC package."""\n')


def _patch_grpc_imports(grpc_file: Path) -> None:
    content = grpc_file.read_text()
    replacements = [
        (
            r"^import environment_pb2 as environment__pb2$",
            f"{GRPC_IMPORT_PREFIX} as environment__pb2",
        ),
        (
            r"^from grl\.environment\.v1 import environment_pb2 as grl_dot_environment_dot_v1_dot_environment__pb2$",
            f"{GRPC_IMPORT_PREFIX} as grl_dot_environment_dot_v1_dot_environment__pb2",
        ),
    ]
    patched = content
    for pattern, replacement in replacements:
        patched = re.sub(
            pattern,
            replacement,
            patched,
            count=1,
            flags=re.MULTILINE,
        )
    grpc_file.write_text(patched)


def main() -> int:
    if not PROTO_FILE.is_file():
        print(f"proto file not found: {PROTO_FILE}", file=sys.stderr)
        return 1

    for directory in [
        PYTHON_OUT,
        PYTHON_OUT / "grl",
        PYTHON_OUT / "grl/environment",
        PYTHON_OUT / "grl/environment/v1",
    ]:
        _ensure_package(directory)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_ROOT}",
        f"--python_out={PYTHON_OUT}",
        f"--pyi_out={PYTHON_OUT}",
        f"--grpc_python_out={PYTHON_OUT}",
        str(PROTO_FILE),
    ]
    subprocess.run(cmd, check=True)

    _patch_grpc_imports(PYTHON_OUT / "grl/environment/v1/environment_pb2_grpc.py")
    print(f"Generated Python stubs under {PYTHON_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
