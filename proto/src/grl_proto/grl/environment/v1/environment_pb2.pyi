from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreateEnvironmentRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class ListTasksRequest(_message.Message):
    __slots__ = ("split",)
    SPLIT_FIELD_NUMBER: _ClassVar[int]
    split: str
    def __init__(self, split: _Optional[str] = ...) -> None: ...

class TaskIndexEntry(_message.Message):
    __slots__ = ("task_id", "split")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SPLIT_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    split: str
    def __init__(self, task_id: _Optional[str] = ..., split: _Optional[str] = ...) -> None: ...

class ListTasksResponse(_message.Message):
    __slots__ = ("tasks", "env_name")
    TASKS_FIELD_NUMBER: _ClassVar[int]
    ENV_NAME_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[TaskIndexEntry]
    env_name: str
    def __init__(self, tasks: _Optional[_Iterable[_Union[TaskIndexEntry, _Mapping]]] = ..., env_name: _Optional[str] = ...) -> None: ...

class CreateEnvironmentResponse(_message.Message):
    __slots__ = ("env_id", "manager_addr", "initial_messages_json", "tools_json")
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    MANAGER_ADDR_FIELD_NUMBER: _ClassVar[int]
    INITIAL_MESSAGES_JSON_FIELD_NUMBER: _ClassVar[int]
    TOOLS_JSON_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    manager_addr: str
    initial_messages_json: str
    tools_json: str
    def __init__(self, env_id: _Optional[str] = ..., manager_addr: _Optional[str] = ..., initial_messages_json: _Optional[str] = ..., tools_json: _Optional[str] = ...) -> None: ...

class EvaluateRequest(_message.Message):
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

class EvaluateResponse(_message.Message):
    __slots__ = ("reward", "detail_json", "infra_error")
    REWARD_FIELD_NUMBER: _ClassVar[int]
    DETAIL_JSON_FIELD_NUMBER: _ClassVar[int]
    INFRA_ERROR_FIELD_NUMBER: _ClassVar[int]
    reward: float
    detail_json: str
    infra_error: bool
    def __init__(self, reward: _Optional[float] = ..., detail_json: _Optional[str] = ..., infra_error: _Optional[bool] = ...) -> None: ...

class ExecuteRequest(_message.Message):
    __slots__ = ("env_id", "tool_name", "arguments_json")
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_JSON_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    tool_name: str
    arguments_json: str
    def __init__(self, env_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., arguments_json: _Optional[str] = ...) -> None: ...

class ExecuteResponse(_message.Message):
    __slots__ = ("content", "is_error")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    content: str
    is_error: bool
    def __init__(self, content: _Optional[str] = ..., is_error: _Optional[bool] = ...) -> None: ...

class TeardownRequest(_message.Message):
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

class TeardownResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
