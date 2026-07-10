"""Guardrails: optional image extras must not load at module import time."""

from __future__ import annotations

import importlib
import sys
import unittest


OPTIONAL_ROOTS = ("torch", "transformers", "vllm", "renderers")


class _BlockOptionalExtras:
    """Import finder that fails if optional GPU/image extras are imported."""

    def find_spec(self, fullname: str, path: object = None, target: object = None):
        root = fullname.split(".", 1)[0]
        if root in OPTIONAL_ROOTS:
            raise ImportError(
                f"optional extra {root!r} must not be imported at module load "
                f"(attempted import of {fullname!r})"
            )
        return None


class ImportIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot modules so later tests keep the same class objects they
        # imported at collection time (dataclass equality is identity-sensitive).
        self._modules_before = dict(sys.modules)
        self._finder = _BlockOptionalExtras()
        sys.meta_path.insert(0, self._finder)
        for name in list(sys.modules):
            if name == "training" or name.startswith("training."):
                del sys.modules[name]
            root = name.split(".", 1)[0]
            if root in OPTIONAL_ROOTS:
                del sys.modules[name]

    def tearDown(self) -> None:
        if self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        for name in list(sys.modules):
            if name not in self._modules_before:
                del sys.modules[name]
        sys.modules.update(self._modules_before)

    def test_head_modules_import_without_optional_extras(self) -> None:
        importlib.import_module("training.types")
        importlib.import_module("training.main")
        importlib.import_module("training.rollouts")
        importlib.import_module("training.trainer")

    def test_trainer_does_not_import_rollouts_module(self) -> None:
        importlib.import_module("training.trainer")
        self.assertNotIn("training.rollouts", sys.modules)


if __name__ == "__main__":
    unittest.main()
