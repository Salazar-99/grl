"""Import smoke tests for generated proto stubs."""

from grl_proto.environment_client import list_task_ids
from grl_proto.grl.environment.v1 import environment_pb2, environment_pb2_grpc


def test_proto_stubs_importable():
    assert environment_pb2.ListTasksRequest is not None
    assert environment_pb2_grpc.EnvironmentServiceStub is not None
    assert callable(list_task_ids)
