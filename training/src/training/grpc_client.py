"""gRPC client for the environment server."""

from __future__ import annotations

import os

import grpc

from training.proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc

DEFAULT_SERVER_ADDR = "localhost:50051"


def server_addr() -> str:
    return os.environ.get("GRL_ENV_SERVER_ADDR", DEFAULT_SERVER_ADDR)


def environment_stub(
    *,
    addr: str | None = None,
    channel: grpc.aio.Channel | None = None,
) -> environment_pb2_grpc.EnvironmentServiceStub:
    if channel is None:
        channel = grpc.aio.insecure_channel(addr or server_addr())
    return environment_pb2_grpc.EnvironmentServiceStub(channel)


async def execute_tool(
    env_id: str,
    tool_name: str,
    arguments: str,
    *,
    addr: str | None = None,
    channel: grpc.aio.Channel | None = None,
) -> dict[str, str]:
    stub = environment_stub(addr=addr, channel=channel)
    response = await stub.Execute(
        environment_pb2.ExecuteRequest(
            env_id=env_id,
            tool_name=tool_name,
            arguments_json=arguments,
        )
    )
    return {"role": "tool", "content": response.content}
