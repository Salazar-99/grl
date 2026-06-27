"""Tests for gRPC retry helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import grpc

from training.environments import (
    InfraError,
    _CREATE_RETRY_CODES,
    _EXECUTE_RETRY_CODES,
    _grpc_retry,
)


class GrpcRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_succeeds_on_first_attempt(self) -> None:
        factory = AsyncMock(return_value="ok")
        result = await _grpc_retry(
            factory,
            max_attempts=3,
            initial_backoff_secs=0.01,
            max_backoff_secs=0.02,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(factory.await_count, 1)

    async def test_retries_unavailable_then_succeeds(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise grpc.aio.AioRpcError(
                    grpc.StatusCode.UNAVAILABLE,
                    None,
                    None,
                    details="env not ready",
                )
            return "ok"

        with patch("training.environments.asyncio.sleep", new=AsyncMock()):
            result = await _grpc_retry(
                factory,
                max_attempts=3,
                initial_backoff_secs=0.01,
                max_backoff_secs=0.02,
            )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)

    async def test_raises_infra_error_when_exhausted(self) -> None:
        async def factory() -> str:
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                None,
                None,
                details="full",
            )

        with patch("training.environments.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(InfraError):
                await _grpc_retry(
                    factory,
                    max_attempts=2,
                    initial_backoff_secs=0.01,
                    max_backoff_secs=0.02,
                )

    async def test_execute_does_not_retry_unavailable(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.UNAVAILABLE,
                None,
                None,
                details="executor closed connection",
            )

        with self.assertRaises(grpc.aio.AioRpcError):
            await _grpc_retry(
                factory,
                max_attempts=3,
                initial_backoff_secs=0.01,
                max_backoff_secs=0.02,
                retry_codes=_EXECUTE_RETRY_CODES,
            )
        self.assertEqual(calls, 1)

    async def test_create_retries_unavailable(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise grpc.aio.AioRpcError(
                    grpc.StatusCode.UNAVAILABLE,
                    None,
                    None,
                    details="env not ready",
                )
            return "ok"

        with patch("training.environments.asyncio.sleep", new=AsyncMock()):
            result = await _grpc_retry(
                factory,
                max_attempts=3,
                initial_backoff_secs=0.01,
                max_backoff_secs=0.02,
                retry_codes=_CREATE_RETRY_CODES,
            )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
