import ray

from training.grpc_client import execute_tool


@ray.remote(resources={"environment": 1})
class EnvironmentWorker:
    def __init__(self, task_id: str, env_id: str) -> None:
        self.task_id = task_id
        self.env_id = env_id

    async def execute(
        self,
        env_id: str,
        tool_name: str,
        arguments: str,
    ) -> dict[str, str]:
        if env_id != self.env_id:
            raise ValueError(f"tool call for env {env_id} routed to env {self.env_id}")

        return await execute_tool(env_id, tool_name, arguments)

    async def reset(self) -> None:
        # TODO: call EnvironmentService.Reset over gRPC.
        return None
