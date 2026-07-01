"""Manager catalog verification via gRPC ListTasks."""

from __future__ import annotations

import asyncio

from grl.config import GRLConfig
from grl.errors import PreflightError
from grl_proto.environment_client import ListTasksError, list_task_ids


def verify_manager_catalog(config: GRLConfig) -> int:
    addr = config.environment.server_addr
    split = config.environment.split
    timeout = config.environment.rpc_timeouts.list_tasks_secs
    try:
        task_ids = asyncio.run(
            list_task_ids(addr=addr, split=split, timeout_secs=timeout)
        )
    except ListTasksError as exc:
        raise PreflightError(str(exc)) from exc
    return len(task_ids)
