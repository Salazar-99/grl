"""Minimal async client helpers for the environment manager gRPC API."""

from __future__ import annotations

import grpc

from grl_proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc


class ListTasksError(RuntimeError):
    """Raised when the manager returns an empty task catalog."""


async def list_task_ids(
    *,
    addr: str,
    split: str | None = None,
    timeout_secs: float = 30.0,
) -> list[str]:
    """Fetch task ids from the manager (catalog loaded at manager startup)."""
    channel = grpc.aio.insecure_channel(addr)
    stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
    try:
        response = await stub.ListTasks(
            environment_pb2.ListTasksRequest(split=split or ""),
            timeout=timeout_secs,
        )
    finally:
        await channel.close()
    if not response.tasks:
        raise ListTasksError(
            f"manager at {addr} returned no tasks"
            + (f" for split {split!r}" if split else "")
        )
    return [entry.task_id for entry in response.tasks]
