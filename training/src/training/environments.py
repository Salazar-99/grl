"""Managed environment sessions via the environment manager gRPC API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import grpc

from training.proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc
from training.retry import InfraError, grpc_retry

SUBMIT_TOOL = "submit"


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 10
    initial_backoff_secs: float = 0.5
    max_backoff_secs: float = 30.0


@dataclass(frozen=True)
class EvaluateResult:
    reward: float
    detail: dict[str, Any]
    infra_error: bool


async def list_task_ids(*, addr: str, split: str | None = None) -> list[str]:
    """Fetch task ids from the manager (catalog loaded at manager startup)."""
    channel = grpc.aio.insecure_channel(addr)
    stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
    try:
        response = await stub.ListTasks(
            environment_pb2.ListTasksRequest(split=split or "")
        )
    finally:
        await channel.close()
    if not response.tasks:
        raise RuntimeError(
            f"manager at {addr} returned no tasks"
            + (f" for split {split!r}" if split else "")
        )
    return [entry.task_id for entry in response.tasks]


class EnvironmentSession:
    """One environment's lifetime on one manager instance.

    Lifecycle: CreateEnvironment → Execute* → Evaluate → Teardown.

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
        retry: RetryConfig,
    ) -> None:
        self._channel = channel
        self._stub = stub
        self.env_id = env_id
        self.task_id = task_id
        self._retry = retry
        self.initial_messages = initial_messages
        self.tools = tools

    @classmethod
    async def create(
        cls,
        task_id: str,
        *,
        addr: str | None = None,
        retry: RetryConfig | None = None,
    ) -> "EnvironmentSession":
        if addr is None:
            raise ValueError("environment server address is required")
        retry_cfg = retry or RetryConfig()

        async def _create_once() -> tuple[grpc.aio.Channel, environment_pb2_grpc.EnvironmentServiceStub, environment_pb2.CreateEnvironmentResponse]:
            channel = grpc.aio.insecure_channel(addr)
            stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
            try:
                response = await stub.CreateEnvironment(
                    environment_pb2.CreateEnvironmentRequest(task_id=task_id)
                )
            except BaseException:
                await channel.close()
                raise
            return channel, stub, response

        channel, stub, response = await grpc_retry(
            _create_once,
            max_attempts=retry_cfg.max_attempts,
            initial_backoff_secs=retry_cfg.initial_backoff_secs,
            max_backoff_secs=retry_cfg.max_backoff_secs,
        )

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
            retry_cfg,
        )

    async def execute(self, tool_name: str, arguments_json: str) -> dict[str, str]:
        async def _execute_once() -> environment_pb2.ExecuteResponse:
            return await self._stub.Execute(
                environment_pb2.ExecuteRequest(
                    env_id=self.env_id,
                    tool_name=tool_name,
                    arguments_json=arguments_json,
                )
            )

        response = await grpc_retry(
            _execute_once,
            max_attempts=self._retry.max_attempts,
            initial_backoff_secs=self._retry.initial_backoff_secs,
            max_backoff_secs=self._retry.max_backoff_secs,
        )
        content = response.content
        if response.is_error:
            content = f"Error: {content}"
        return {"role": "tool", "content": content}

    async def evaluate(self) -> EvaluateResult:
        """Grade the finished trajectory. Callable once per env after Execute."""

        async def _evaluate_once() -> environment_pb2.EvaluateResponse:
            return await self._stub.Evaluate(
                environment_pb2.EvaluateRequest(env_id=self.env_id)
            )

        response = await grpc_retry(
            _evaluate_once,
            max_attempts=self._retry.max_attempts,
            initial_backoff_secs=self._retry.initial_backoff_secs,
            max_backoff_secs=self._retry.max_backoff_secs,
        )
        detail = json.loads(response.detail_json) if response.detail_json else {}
        return EvaluateResult(
            reward=response.reward,
            detail=detail,
            infra_error=response.infra_error,
        )

    async def teardown(self) -> None:
        try:
            await self._stub.Teardown(
                environment_pb2.TeardownRequest(env_id=self.env_id)
            )
        finally:
            await self._channel.close()


__all__ = ["EnvironmentSession", "EvaluateResult", "InfraError", "RetryConfig", "SUBMIT_TOOL", "list_task_ids"]
