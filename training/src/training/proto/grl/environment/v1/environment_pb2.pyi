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
    __slots__ = ("env_id",)
    ENV_ID_FIELD_NUMBER: _ClassVar[int]
    env_id: str
    def __init__(self, env_id: _Optional[str] = ...) -> None: ...

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
