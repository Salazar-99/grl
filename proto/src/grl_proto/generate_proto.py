"""Generate Python gRPC stubs from the shared proto definitions."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROTO_REL = Path("environments/proto/grl/environment/v1/environment.proto")
GRPC_IMPORT_PREFIX = "from grl_proto.grl.environment.v1 import environment_pb2"


def _repo_root() -> Path:
    for start in (Path(__file__).resolve(), Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / PROTO_REL).is_file():
                return candidate
    raise FileNotFoundError(
        f"could not locate {PROTO_REL}; run generate-proto from the grl repo"
    )


def _python_out(repo_root: Path) -> Path:
    return repo_root / "proto/src/grl_proto"


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
    repo_root = _repo_root()
    proto_root = repo_root / "environments/proto"
    proto_file = repo_root / PROTO_REL
    python_out = _python_out(repo_root)

    for directory in [
        python_out,
        python_out / "grl",
        python_out / "grl/environment",
        python_out / "grl/environment/v1",
    ]:
        _ensure_package(directory)

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{proto_root}",
        f"--python_out={python_out}",
        f"--pyi_out={python_out}",
        f"--grpc_python_out={python_out}",
        str(proto_file),
    ]
    subprocess.run(cmd, check=True)

    _patch_grpc_imports(python_out / "grl/environment/v1/environment_pb2_grpc.py")
    print(f"Generated Python stubs under {python_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
