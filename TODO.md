Remaining work: small model training on SWE-bench-Lite
=======================================================

Goal: one full RL run — rollout → reward → GRPO step → weight update reaches
vLLM — with production-quality correctness, performance, and observability.
Scaling and fault tolerance are explicitly deferred (see bottom). General
reference material lives in NOTES.md.

swebench-lite
-------------
- [ ] Review for coupling between environment and grl

Training correctness
--------------------
- [ ] Token-In-Token-Out semantics (e.g. prime renderers lib): keep exact
      sampled token ids and logprobs end-to-end so the trainer sees what the
      sampler emitted — no retokenization drift between vLLM and HF.
- [ ] Fix tool-message tokenization: `_tokenize_tool_messages` templates tool
      messages in isolation, re-prepending system/BOS tokens mid-trajectory.
      Render the full message list incrementally and slice. (Subsumed by a
      proper TITO renderer if adopted.)

Performance
-----------
- [ ] Weight sync (currently `return None` — without it this isn't RL):
      start with object-store state dict + in-place vLLM reload, measure
      sync latency.
- [ ] Sparse weight transfer: send only a delta of the weights from trainer
      to rollout workers (Cursor-style) to cut sync time and keep rollouts
      closer to on-policy.
- [ ] (For multiple rollout engines) Exploit prefix caching deliberately: the N rollouts in a group share a
      prompt — route them to the same engine using a hashmap in the data submitter

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

Deploy / glue
-------------
- [ ] Implement the launcher to upload the environment artifacts, deploy the infra, and deploy/run the training job using python, see prime-rl for example
- [ ] Get model weights onto GPU nodes, maybe a daemonset that pulls from HF? or prebake into image?

Deferred (scaling / fault tolerance)
------------------------------------
- Multi-node training engine (FSDP/Megatron), multi-engine rollout fleet
- NCCL/RDMA weight broadcast
- Checkpointing + resumption, retries, preemption tolerance
- Off-policy corrections (importance sampling, interruptible generation)
- VM snapshot/restore for fast resets; manager autoscaling / load-aware
  placement
