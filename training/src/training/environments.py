"""Managed environment sessions via the environment manager gRPC API."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

import grpc

from grl_config.training import EnvironmentRpcTimeoutsConfig
from grl_proto.environment_client import list_task_ids as _list_task_ids_raw
from grl_proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc
from training.telemetry import counter, gauge, histogram, span

# Open environment sessions on this process (create succeeded, teardown not yet
# run). asyncio is single-threaded per Ray actor, so a plain int is race-free;
# the gauge is re-set on every change so it tracks the live count.
_active_sessions = 0


def _set_active_sessions(delta: int) -> None:
    global _active_sessions
    _active_sessions += delta
    gauge("grl.env.active").set(_active_sessions)


@contextmanager
def _record_env_rpc_duration(rpc: str) -> Iterator[None]:
    """Time one env RPC and tag the histogram with success vs failure."""
    start = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        histogram("grl.env.rpc.duration", unit="s").record(
            time.perf_counter() - start, {"rpc": rpc, "ok": ok}
        )


T = TypeVar("T")

# Retried while the manager/env is temporarily unavailable (boot, admission).
_RETRYABLE_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)

# CreateEnvironment while the VM is still booting.
_CREATE_RETRY_CODES = _RETRYABLE_CODES

# Execute/Evaluate after the env is ready — only admission pressure is retried.
_EXECUTE_RETRY_CODES = frozenset({grpc.StatusCode.RESOURCE_EXHAUSTED})
_EVALUATE_RETRY_CODES = _EXECUTE_RETRY_CODES


class InfraError(Exception):
    """Environment infrastructure failure after retries are exhausted."""


async def _grpc_retry(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    initial_backoff_secs: float,
    max_backoff_secs: float,
    retry_codes: frozenset[grpc.StatusCode] = _RETRYABLE_CODES,
    rpc: str = "",
) -> T:
    """Run an async gRPC call, retrying retryable status codes with backoff."""
    backoff = initial_backoff_secs
    last_exc: BaseException | None = None

    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except grpc.aio.AioRpcError as exc:
            last_exc = exc
            code = exc.code()
            if code not in retry_codes or attempt + 1 >= max_attempts:
                if code in retry_codes:
                    counter("grl.env.infra_errors").add(1, {"rpc": rpc})
                    raise InfraError(str(exc)) from exc
                counter("grl.env.rpc.errors").add(1, {"rpc": rpc, "code": code.name})
                raise
            counter("grl.env.rpc.retries").add(1, {"rpc": rpc})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_secs)

    assert last_exc is not None
    counter("grl.env.infra_errors").add(1, {"rpc": rpc})
    raise InfraError(str(last_exc)) from last_exc


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
    with span("env.list_tasks"), _record_env_rpc_duration("list_tasks"):
        return await _list_task_ids_raw(
            addr=addr,
            split=split,
            timeout_secs=timeouts.list_tasks_secs,
        )


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

        with span("env.create", task_id=task_id), _record_env_rpc_duration("create"):
            channel, stub, response = await _grpc_retry(
                _create_once,
                max_attempts=retry_cfg.max_attempts,
                initial_backoff_secs=retry_cfg.initial_backoff_secs,
                max_backoff_secs=retry_cfg.max_backoff_secs,
                retry_codes=_CREATE_RETRY_CODES,
                rpc="create",
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
        _set_active_sessions(1)
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

        with span("env.execute", tool=tool_name), _record_env_rpc_duration("execute"):
            response = await _grpc_retry(
                _execute_once,
                max_attempts=self._retry.max_attempts,
                initial_backoff_secs=self._retry.initial_backoff_secs,
                max_backoff_secs=self._retry.max_backoff_secs,
                retry_codes=_EXECUTE_RETRY_CODES,
                rpc="execute",
            )
        counter("grl.env.tool.calls").add(
            1, {"tool": tool_name, "is_error": bool(response.is_error)}
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

        with span("env.evaluate"), _record_env_rpc_duration("evaluate"):
            response = await _grpc_retry(
                _evaluate_once,
                max_attempts=self._retry.max_attempts,
                initial_backoff_secs=self._retry.initial_backoff_secs,
                max_backoff_secs=self._retry.max_backoff_secs,
                retry_codes=_EVALUATE_RETRY_CODES,
                rpc="evaluate",
            )
        detail = json.loads(response.detail_json) if response.detail_json else {}
        return EvaluateResult(
            reward=response.reward,
            detail=detail,
            infra_error=response.infra_error,
        )

    async def teardown(self) -> None:
        try:
            with span("env.teardown"), _record_env_rpc_duration("teardown"):
                await self._stub.Teardown(
                    environment_pb2.TeardownRequest(env_id=self.env_id),
                    timeout=self._rpc.teardown_secs,
                )
        finally:
            _set_active_sessions(-1)
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
