# grl-proto

Python gRPC stubs and client helpers for the GRL environment API.

The protobuf source of truth is
[`../environments/proto/grl/environment/v1/environment.proto`](../environments/proto/grl/environment/v1/environment.proto).

## Codegen

```bash
cd proto
uv sync --group dev
uv run generate-proto
```

This writes generated stubs into `src/grl_proto/grl/environment/v1/`.
