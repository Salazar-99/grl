"""Tests for model id and cache path helpers."""

from __future__ import annotations

import os
import unittest

from training.model import local_model_path


class ModelPathTests(unittest.TestCase):
    def test_local_model_path_from_hf_id(self) -> None:
        self.assertEqual(str(local_model_path("Qwen/Qwen3.5-4B")), "/models/Qwen3.5-4B")

    def test_env_override_cache_root(self) -> None:
        previous = os.environ.get("GRL_MODEL_CACHE_ROOT")
        try:
            os.environ["GRL_MODEL_CACHE_ROOT"] = "/mnt/models"
            self.assertEqual(str(local_model_path("Qwen/Qwen3.5-4B")), "/mnt/models/Qwen3.5-4B")
        finally:
            if previous is None:
                os.environ.pop("GRL_MODEL_CACHE_ROOT", None)
            else:
                os.environ["GRL_MODEL_CACHE_ROOT"] = previous


class RendererSelectionTests(unittest.TestCase):
    def test_renderer_name_for_qwen35(self) -> None:
        from training.rollouts import Renderer

        self.assertEqual(Renderer.name_for_model("Qwen/Qwen3.5-4B"), "qwen3.5")
        self.assertEqual(Renderer.name_for_model("org/unmapped-model"), "default")


if __name__ == "__main__":
    unittest.main()
