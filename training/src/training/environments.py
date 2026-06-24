"""Managed environment sessions via the environment manager gRPC API."""

from __future__ import annotations

import json
from typing import Any

import grpc

from training.proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc


class EnvironmentSession:
    """One environment's lifetime on one manager instance.

    Each session owns a dedicated channel. A gRPC channel is a single TCP
    connection, so every call for this environment reaches the manager pod
    that created it, even behind a ClusterIP Service — kube-proxy balances
    per-connection, which also spreads environments across manager pods.
    """

    def __init__(
        self,
        channel: grpc.aio.Channel,
        stub: environment_pb2_grpc.EnvironmentServiceStub,
        env_id: str,
        task_id: str,
        initial_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> None:
        self._channel = channel
        self._stub = stub
        self.env_id = env_id
        self.task_id = task_id
        # The environment renders the opening prompt and tool schemas, so the
        # trainer never parses task-specific data — it just drives the policy
        # with whatever the env hands back.
        self.initial_messages = initial_messages
        self.tools = tools

    @classmethod
    async def create(
        cls,
        task_id: str,
        *,
        addr: str | None = None,
    ) -> "EnvironmentSession":
        if addr is None:
            raise ValueError("environment server address is required")
        channel = grpc.aio.insecure_channel(addr)
        stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
        try:
            response = await stub.CreateEnvironment(
                environment_pb2.CreateEnvironmentRequest(task_id=task_id)
            )
        except BaseException:
            await channel.close()
            raise
        # Behind the Service, pinning is only as durable as the TCP connection:
        # a transparent gRPC reconnect would be re-balanced to a pod that does
        # not own this VM. Re-dial the owning pod directly when it tells us
        # where it lives.
        if response.manager_addr and response.manager_addr != addr:
            await channel.close()
            channel = grpc.aio.insecure_channel(response.manager_addr)
            stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
        initial_messages = (
            json.loads(response.initial_messages_json)
            if response.initial_messages_json
            else []
        )
        tools = json.loads(response.tools_json) if response.tools_json else None
        return cls(
            channel,
            stub,
            response.env_id,
            task_id,
            initial_messages,
            tools,
        )

    async def execute(self, tool_name: str, arguments_json: str) -> dict[str, str]:
        response = await self._stub.Execute(
            environment_pb2.ExecuteRequest(
                env_id=self.env_id,
                tool_name=tool_name,
                arguments_json=arguments_json,
            )
        )
        return {"role": "tool", "content": response.content}

    async def score(self) -> tuple[float, dict[str, Any]]:
        """Ask the environment to grade the current state and return the reward.

        The environment owns the reward (e.g. running the held-out test suite),
        so the trainer treats this as opaque: a scalar plus an optional JSON
        breakdown for logging.
        """
        response = await self._stub.Score(
            environment_pb2.ScoreRequest(env_id=self.env_id)
        )
        detail = json.loads(response.detail_json) if response.detail_json else {}
        return response.reward, detail

    async def reset(self) -> None:
        await self._stub.Reset(environment_pb2.ResetRequest(env_id=self.env_id))

    async def close(self) -> None:
        try:
            await self._stub.Close(environment_pb2.CloseRequest(env_id=self.env_id))
        finally:
            await self._channel.close()
