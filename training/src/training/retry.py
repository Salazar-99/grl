"""gRPC retry helpers with exponential backoff."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import grpc

T = TypeVar("T")

RETRYABLE_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)


class InfraError(Exception):
    """Environment infrastructure failure after retries are exhausted."""


async def grpc_retry(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    initial_backoff_secs: float,
    max_backoff_secs: float,
    retry_codes: frozenset[grpc.StatusCode] = RETRYABLE_CODES,
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
                    raise InfraError(str(exc)) from exc
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_secs)

    assert last_exc is not None
    raise InfraError(str(last_exc)) from last_exc
