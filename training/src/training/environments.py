"""Managed environment sessions via the environment manager gRPC API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import grpc

from training.config import EnvironmentRpcTimeoutsConfig
from training.proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc
from training.retry import (
    CREATE_RETRY_CODES,
    EVALUATE_RETRY_CODES,
    EXECUTE_RETRY_CODES,
    InfraError,
    grpc_retry,
)

SUBMIT_TOOL = "submit"
DEFAULT_TOOL_TIMEOUT_SECS = 120


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 10
    initial_backoff_secs: float = 0.5
    max_backoff_secs: float = 30.0


@dataclass(frozen=True)
class RpcTimeouts:
    create_secs: float
    list_tasks_secs: float
    execute_default_secs: float
    execute_submit_secs: float
    execute_timeout_buffer_secs: float
    evaluate_secs: float
    teardown_secs: float

    @classmethod
    def from_config(cls, cfg: EnvironmentRpcTimeoutsConfig) -> RpcTimeouts:
        return cls(
            create_secs=cfg.create_secs,
            list_tasks_secs=cfg.list_tasks_secs,
            execute_default_secs=cfg.execute_default_secs,
            execute_submit_secs=cfg.execute_submit_secs,
            execute_timeout_buffer_secs=cfg.execute_timeout_buffer_secs,
            evaluate_secs=cfg.evaluate_secs,
            teardown_secs=cfg.teardown_secs,
        )

    def execute_secs(self, tool_name: str, arguments_json: str) -> float:
        if tool_name == SUBMIT_TOOL:
            return self.execute_submit_secs
        tool_timeout = _tool_timeout_secs(arguments_json)
        return float(tool_timeout) + self.execute_timeout_buffer_secs


def _tool_timeout_secs(arguments_json: str) -> int:
    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return DEFAULT_TOOL_TIMEOUT_SECS
    raw = args.get("timeout_secs")
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, float) and raw > 0:
        return int(raw)
    return DEFAULT_TOOL_TIMEOUT_SECS


@dataclass(frozen=True)
class EvaluateResult:
    reward: float
    detail: dict[str, Any]
    infra_error: bool


async def list_task_ids(
    *,
    addr: str,
    split: str | None = None,
    rpc_timeouts: RpcTimeouts | None = None,
) -> list[str]:
    """Fetch task ids from the manager (catalog loaded at manager startup)."""
    timeouts = rpc_timeouts or RpcTimeouts.from_config(EnvironmentRpcTimeoutsConfig())
    channel = grpc.aio.insecure_channel(addr)
    stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
    try:
        response = await stub.ListTasks(
            environment_pb2.ListTasksRequest(split=split or ""),
            timeout=timeouts.list_tasks_secs,
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
        rpc_timeouts: RpcTimeouts,
    ) -> None:
        self._channel = channel
        self._stub = stub
        self.env_id = env_id
        self.task_id = task_id
        self._retry = retry
        self._rpc = rpc_timeouts
        self.initial_messages = initial_messages
        self.tools = tools

    @classmethod
    async def create(
        cls,
        task_id: str,
        *,
        addr: str | None = None,
        retry: RetryConfig | None = None,
        rpc_timeouts: RpcTimeouts | None = None,
    ) -> EnvironmentSession:
        if addr is None:
            raise ValueError("environment server address is required")
        retry_cfg = retry or RetryConfig()
        rpc_cfg = rpc_timeouts or RpcTimeouts.from_config(EnvironmentRpcTimeoutsConfig())

        async def _create_once() -> tuple[
            grpc.aio.Channel,
            environment_pb2_grpc.EnvironmentServiceStub,
            environment_pb2.CreateEnvironmentResponse,
        ]:
            channel = grpc.aio.insecure_channel(addr)
            stub = environment_pb2_grpc.EnvironmentServiceStub(channel)
            try:
                response = await stub.CreateEnvironment(
                    environment_pb2.CreateEnvironmentRequest(task_id=task_id),
                    timeout=rpc_cfg.create_secs,
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
            retry_codes=CREATE_RETRY_CODES,
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
            rpc_cfg,
        )

    async def execute(self, tool_name: str, arguments_json: str) -> dict[str, str]:
        timeout = self._rpc.execute_secs(tool_name, arguments_json)

        async def _execute_once() -> environment_pb2.ExecuteResponse:
            return await self._stub.Execute(
                environment_pb2.ExecuteRequest(
                    env_id=self.env_id,
                    tool_name=tool_name,
                    arguments_json=arguments_json,
                ),
                timeout=timeout,
            )

        response = await grpc_retry(
            _execute_once,
            max_attempts=self._retry.max_attempts,
            initial_backoff_secs=self._retry.initial_backoff_secs,
            max_backoff_secs=self._retry.max_backoff_secs,
            retry_codes=EXECUTE_RETRY_CODES,
        )
        content = response.content
        if response.is_error:
            content = f"Error: {content}"
        return {"role": "tool", "content": content}

    async def evaluate(self) -> EvaluateResult:
        """Grade the finished trajectory. Callable once per env after Execute."""

        async def _evaluate_once() -> environment_pb2.EvaluateResponse:
            return await self._stub.Evaluate(
                environment_pb2.EvaluateRequest(env_id=self.env_id),
                timeout=self._rpc.evaluate_secs,
            )

        response = await grpc_retry(
            _evaluate_once,
            max_attempts=self._retry.max_attempts,
            initial_backoff_secs=self._retry.initial_backoff_secs,
            max_backoff_secs=self._retry.max_backoff_secs,
            retry_codes=EVALUATE_RETRY_CODES,
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
                environment_pb2.TeardownRequest(env_id=self.env_id),
                timeout=self._rpc.teardown_secs,
            )
        finally:
            await self._channel.close()


__all__ = [
    "EnvironmentSession",
    "EvaluateResult",
    "InfraError",
    "RetryConfig",
    "RpcTimeouts",
    "SUBMIT_TOOL",
    "list_task_ids",
]
