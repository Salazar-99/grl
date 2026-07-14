"""The policy must be loaded under the checkpoint's own architecture.

The rollout engine repopulates its parameters from the names in this state dict and
leaves anything it was not given on the meta device. Loading a
`*ForConditionalGeneration` checkpoint with AutoModelForCausalLM silently yields the
text-only tower — parameters named `model.*` instead of `model.language_model.*`,
with the vision tower absent — which strands the real embedding on meta and kills the
engine on its next forward pass.
"""

import json
import unittest
from unittest.mock import patch

import torch

from training.trainer import assert_covers_checkpoint, load_policy_model


class FakeConfig:
    def __init__(self, architectures):
        self.architectures = architectures


class FakeModel(torch.nn.Module):
    def __init__(self, names):
        super().__init__()
        for name in names:
            self.register_parameter(
                name.replace(".", "_"), torch.nn.Parameter(torch.zeros(1))
            )
        self._names = names

    def state_dict(self, *args, **kwargs):
        return {name: torch.zeros(1) for name in self._names}

    def to(self, *args, **kwargs):
        return self


class LoadPolicyModelTests(unittest.TestCase):
    def _load(self, architectures, names=("model.language_model.embed_tokens.weight",)):
        loaded = {}

        class FakeAuto:
            def __init__(self, label):
                self.label = label

            def from_pretrained(self, path, **kwargs):
                loaded["class"] = self.label
                return FakeModel(list(names))

        with (
            patch(
                "transformers.AutoConfig.from_pretrained",
                return_value=FakeConfig(architectures),
            ),
            patch("transformers.AutoModelForImageTextToText", FakeAuto("image_text")),
            patch("transformers.AutoModelForCausalLM", FakeAuto("causal_lm")),
        ):
            load_policy_model("/models/fake")
        return loaded["class"]

    def test_conditional_generation_checkpoint_uses_multimodal_class(self) -> None:
        """The exact bug: this checkpoint must not load as text-only."""
        self.assertEqual(
            self._load(["Qwen3_5ForConditionalGeneration"]), "image_text"
        )

    def test_plain_causal_lm_checkpoint_still_uses_causal_lm_class(self) -> None:
        self.assertEqual(self._load(["Qwen3ForCausalLM"]), "causal_lm")

    def test_missing_architectures_falls_back_to_causal_lm(self) -> None:
        self.assertEqual(self._load(None), "causal_lm")


class CheckpointCoverageTests(unittest.TestCase):
    def _write_index(self, tmp, names):
        from pathlib import Path

        index = Path(tmp) / "model.safetensors.index.json"
        index.write_text(json.dumps({"weight_map": {n: "shard.safetensors" for n in names}}))
        return tmp

    def test_raises_when_the_model_omits_checkpoint_weights(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_index(
                tmp,
                ["model.language_model.embed_tokens.weight", "model.visual.patch_embed.weight"],
            )
            # What AutoModelForCausalLM produced: text-only names, no vision tower.
            model = FakeModel(["model.embed_tokens.weight"])

            with self.assertRaises(RuntimeError) as ctx:
                assert_covers_checkpoint(model, tmp)

            self.assertIn("meta device", str(ctx.exception))

    def test_accepts_a_model_that_covers_the_checkpoint(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            names = ["model.language_model.embed_tokens.weight", "model.visual.patch_embed.weight"]
            self._write_index(tmp, names)
            # A superset is fine: tied heads appear in the model but not the checkpoint.
            model = FakeModel([*names, "lm_head.weight"])

            assert_covers_checkpoint(model, tmp)

    def test_no_index_file_is_not_an_error(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            assert_covers_checkpoint(FakeModel(["anything"]), tmp)


if __name__ == "__main__":
    unittest.main()
