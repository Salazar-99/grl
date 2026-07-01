# Training

RL loop orchestration (Ray rollouts, rewards, model updates).

Training configuration lives in the shared [`grl_config`](../config/) package. Python gRPC stubs live in [`grl_proto`](../proto/).

## Proto

Python gRPC stubs are generated from the shared contract at [`../environments/proto/grl/environment/v1/environment.proto`](../environments/proto/grl/environment/v1/environment.proto).

```bash
cd ../proto
uv sync --group dev
uv run generate-proto
```

This writes into `proto/src/grl_proto/`.

## Images

The Dockerfile builds the `training` package into role-specific images. This way when we use Ray to spin up a remote actor
it runs in a container spawned from an image with the minimal dependency set needed for that actor's work.

```sh
docker build -f training/Dockerfile --target head -t grl-training:head .
docker build -f training/Dockerfile --target training -t grl-training:training .
docker build -f training/Dockerfile --target rollouts -t grl-training:rollouts .
```
