from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class CreateEnvironmentRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

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

class ScoreRequest(_message.Message):
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

class ScoreResponse(_message.Message):
    __slots__ = ("reward", "detail_json")
    REWARD_FIELD_NUMBER: _ClassVar[int]
    DETAIL_JSON_FIELD_NUMBER: _ClassVar[int]
    reward: float
    detail_json: str
    def __init__(self, reward: _Optional[float] = ..., detail_json: _Optional[str] = ...) -> None: ...

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

class ResetRequest(_message.Message):
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

class ResetResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CloseRequest(_message.Message):
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

class CloseResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
