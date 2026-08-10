"""Control-plane tests for the selectable weight-sync backend."""

from __future__ import annotations

import asyncio

from training import main


class _RemoteMethod:
    def __init__(self, result=None):
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def remote(self, *args):
        self.calls.append(args)
        return self.result


class _TrainingWorker:
    def __init__(self):
        self.nccl_rendezvous = _RemoteMethod(("10.0.0.8", 41231))
        self.init_nccl_weight_transfer = _RemoteMethod(None)


class _RolloutWorker:
    def __init__(self):
        self.init_nccl_weight_transfer = _RemoteMethod(None)


def test_payload_uses_effective_backend_without_mutating_config():
    class Config:
        def __init__(self):
            self.payload = {"weight_sync": {"backend": "auto"}}

        def model_dump(self):
            return {"weight_sync": dict(self.payload["weight_sync"])}

    config = Config()
    payload = main._weight_sync_payload(config, "ray")
    assert payload["weight_sync"]["backend"] == "ray"
    assert config.payload["weight_sync"]["backend"] == "auto"


def test_nccl_initialization_assigns_distinct_tp_rank_ranges(monkeypatch):
    trainer = _TrainingWorker()
    rollouts = [_RolloutWorker(), _RolloutWorker()]

    async def immediate(value):
        return value

    monkeypatch.setattr(main, "_get", immediate)
    asyncio.run(
        main._initialize_nccl_weight_sync(
            trainer, rollouts, tensor_parallel_size=2
        )
    )

    assert rollouts[0].init_nccl_weight_transfer.calls == [
        ("10.0.0.8", 41231, 1, 5)
    ]
    assert rollouts[1].init_nccl_weight_transfer.calls == [
        ("10.0.0.8", 41231, 3, 5)
    ]
    assert trainer.init_nccl_weight_transfer.calls == [("10.0.0.8", 41231, 5)]
