"""The micro-batched, chunked training step must not change the update.

Both are equivalence tests against the straightforward whole-batch implementation
that these optimizations replaced: chunking the vocab projection and splitting the
batch are memory optimizations, so identical numbers (and identical gradients) are
the entire contract.
"""

import unittest

import torch
import torch.nn.functional as F

from training.trainer import TrainingWorker, chunked_logprobs


def naive_logprobs(lm_head, hidden, input_ids):
    """The whole-batch form: project everything, then reduce."""
    logits = lm_head(hidden[:, :-1, :]).float()
    log_probs = F.log_softmax(logits, dim=-1)
    labels = input_ids[:, 1:]
    gathered = log_probs.gather(-1, labels.unsqueeze(-1)).squeeze(-1)
    entropy = -(log_probs.exp() * log_probs).sum(dim=-1)
    return gathered, entropy


class ChunkedLogprobsTests(unittest.TestCase):
    def setUp(self) -> None:
        torch.manual_seed(0)
        self.batch, self.seq, self.hidden_size, self.vocab = 2, 9, 4, 16
        self.lm_head = torch.nn.Linear(self.hidden_size, self.vocab, bias=False)
        self.input_ids = torch.randint(0, self.vocab, (self.batch, self.seq))

    def _hidden(self) -> torch.Tensor:
        return torch.randn(
            self.batch, self.seq, self.hidden_size, requires_grad=True
        )

    def test_matches_naive_logprobs_and_entropy(self) -> None:
        hidden = self._hidden()

        expected_lp, expected_ent = naive_logprobs(self.lm_head, hidden, self.input_ids)
        # A chunk size that does not divide the sequence exercises the ragged tail.
        actual_lp, actual_ent = chunked_logprobs(
            self.lm_head, hidden, self.input_ids, chunk_size=3
        )

        torch.testing.assert_close(actual_lp, expected_lp)
        torch.testing.assert_close(actual_ent, expected_ent)

    def test_chunk_size_does_not_change_result(self) -> None:
        hidden = self._hidden()
        reference, _ = chunked_logprobs(
            self.lm_head, hidden, self.input_ids, chunk_size=1
        )
        for chunk_size in (2, 5, self.seq, self.seq * 2):
            actual, _ = chunked_logprobs(
                self.lm_head, hidden, self.input_ids, chunk_size=chunk_size
            )
            with self.subTest(chunk_size=chunk_size):
                torch.testing.assert_close(actual, reference)

    def test_gradients_match_naive_implementation(self) -> None:
        """Checkpointed recompute must reproduce the naive gradients exactly."""
        hidden = self._hidden()
        naive_logprobs(self.lm_head, hidden, self.input_ids)[0].sum().backward()
        expected_hidden_grad = hidden.grad.clone()
        expected_head_grad = self.lm_head.weight.grad.clone()

        self.lm_head.zero_grad()
        chunked_hidden = hidden.detach().clone().requires_grad_(True)
        chunked_logprobs(
            self.lm_head, chunked_hidden, self.input_ids, chunk_size=3
        )[0].sum().backward()

        torch.testing.assert_close(chunked_hidden.grad, expected_hidden_grad)
        torch.testing.assert_close(self.lm_head.weight.grad, expected_head_grad)


class TinyCausalLM(torch.nn.Module):
    """Smallest thing satisfying the decoder/output-embeddings contract the trainer uses."""

    def __init__(self, vocab: int, hidden_size: int) -> None:
        super().__init__()
        self.embed = torch.nn.Embedding(vocab, hidden_size)
        self.head = torch.nn.Linear(hidden_size, vocab, bias=False)

    def get_decoder(self):
        model = self

        class Decoder:
            def __call__(self, *, input_ids, attention_mask):
                class Output:
                    last_hidden_state = model.embed(input_ids)

                return Output()

        return Decoder()

    def get_output_embeddings(self):
        return self.head


class MicroBatchEquivalenceTests(unittest.TestCase):
    """Accumulated micro-batch gradients must equal the whole-batch gradient."""

    def _worker(self, micro_batch_size: int) -> TrainingWorker:
        worker_cls = TrainingWorker.__ray_metadata__.modified_class
        worker = object.__new__(worker_cls)
        worker.beta = 0.001
        worker.epsilon = 0.2
        worker.loss_scale_factor = None
        worker.micro_batch_size = micro_batch_size
        worker.logprob_chunk_size = 3
        worker.model = self.model
        return worker

    def setUp(self) -> None:
        torch.manual_seed(0)
        vocab, hidden_size = 16, 4
        self.model = TinyCausalLM(vocab, hidden_size)

        batch, seq, max_resp = 4, 9, 5
        self.tensors = {
            "input_ids": torch.randint(0, vocab, (batch, seq)),
            "attention_mask": torch.ones(batch, seq, dtype=torch.bool),
            "prompt_lens": torch.tensor([3, 4, 3, 2]),
            "response_lens": torch.tensor([5, 4, 3, 5]),
            "inference_logprobs": torch.randn(batch, max_resp),
            "response_mask": torch.zeros(batch, max_resp, dtype=torch.bool),
            "advantages": torch.tensor([1.0, -1.0, 0.5, -0.5]),
        }
        for i, length in enumerate(self.tensors["response_lens"]):
            self.tensors["response_mask"][i, :length] = True

    def _run(self, micro_batch_size: int):
        self.model.zero_grad(set_to_none=True)
        worker = self._worker(micro_batch_size)
        loss, stats, entropy = worker._accumulate_gradients(self.tensors)
        grads = {
            name: param.grad.clone()
            for name, param in self.model.named_parameters()
            if param.grad is not None
        }
        return loss, stats, entropy, grads

    def test_micro_batching_matches_whole_batch(self) -> None:
        batch_size = int(self.tensors["input_ids"].shape[0])
        whole_loss, whole_stats, whole_entropy, whole_grads = self._run(batch_size)

        for micro_batch_size in (1, 2, 3):
            loss, stats, entropy, grads = self._run(micro_batch_size)
            with self.subTest(micro_batch_size=micro_batch_size):
                self.assertAlmostEqual(loss, whole_loss, places=5)
                self.assertAlmostEqual(entropy, whole_entropy, places=5)
                for key, value in whole_stats.items():
                    self.assertAlmostEqual(stats[key], value, places=5, msg=key)
                self.assertEqual(set(grads), set(whole_grads))
                for name, grad in whole_grads.items():
                    torch.testing.assert_close(
                        grads[name], grad, rtol=1e-4, atol=1e-6, msg=name
                    )

    def test_padding_beyond_the_response_does_not_reach_the_loss(self) -> None:
        """A batch padded to a longer width must produce the same gradients."""
        _, _, _, grads = self._run(1)

        padded = {key: value.clone() for key, value in self.tensors.items()}
        pad = torch.zeros(4, 3, dtype=torch.bool)
        padded["response_mask"] = torch.cat([padded["response_mask"], pad], dim=1)
        padded["inference_logprobs"] = torch.cat(
            [padded["inference_logprobs"], torch.randn(4, 3)], dim=1
        )
        self.tensors = padded
        _, _, _, padded_grads = self._run(1)

        # loss_scale tracks the padded width, so gradients scale by the width ratio.
        scale = 5 / 8
        for name, grad in grads.items():
            torch.testing.assert_close(
                padded_grads[name], grad * scale, rtol=1e-4, atol=1e-6, msg=name
            )


if __name__ == "__main__":
    unittest.main()
