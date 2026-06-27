Remaining work: small model training on SWE-bench-Lite
=======================================================

Goal: one full RL run — rollout → reward → GRPO step → weight update reaches
vLLM — with production-quality correctness, performance, and observability.
Scaling and fault tolerance are explicitly deferred (see bottom). General
reference material lives in NOTES.md.

Create and upload environment bundle for swebench-lite

Observability
-------------
See NOTES.md for the pipeline layout, metric catalog, and ClickHouse schema
notes.
- [ ] Emit the catalog metrics/spans from real call sites using the
      `training.telemetry` helpers — they exist but nothing calls them yet. Tag
      with policy_version (run.id + per-role `init_telemetry` + OTLP export to
      the in-cluster collector are already wired). Then add equivalent OTel
      instrumentation to the Rust manager (`environments/manager/`).
- [ ] Figure out how to store and query trajectories
- [ ] Implement a single Grafana dashboard as provisioned JSON checked into
      the repo (e.g. `infra/observability/grafana/grl-dashboard.json`), rows:
      Training, Rollouts/vLLM, Pipeline, Environment, GPU/Ray, Evals — all
      panels backed by the ClickHouse datasource. Include a trajectory-
      browser table panel over `grl.trajectories`.

Launcher
---------
- Switching logic to deploy cluster or not
- Logic to deploy cluster
- Logic to kickoff training
- Logic to kickoff new training on existing cluster

Deferred (scaling / fault tolerance)
------------------------------------
- (For multiple rollout engines) Exploit prefix caching deliberately: the N rollouts in a group share a
      prompt — route them to the same engine using a hashmap in the data submitter
- Multi-node training engine (FSDP/Megatron), multi-engine rollout fleet
- NCCL/RDMA weight broadcast
- Checkpointing + resumption, retries, preemption tolerance
- Off-policy corrections (importance sampling, interruptible generation)
- VM snapshot/restore for fast resets; manager autoscaling / load-aware
  placement
- (At low precision) Sparse weight transfer: send only a delta of the weights from trainer
      to rollout workers (Cursor-style) to cut sync time and keep rollouts
      closer to on-policy.
- Implement a real distributed learner topology before scaling training
      beyond one `TrainingWorker`. Multiple training GPUs should cooperate as
      ranks in one logical learner with one optimizer/policy-version stream,
      not independent actors processing batches separately.
