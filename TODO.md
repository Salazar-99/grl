Remaining work: small model training on SWE-bench-Lite
=======================================================

Goal: one full RL run — rollout → reward → GRPO step → weight update reaches
vLLM — with production-quality correctness, performance, and observability.
Scaling and fault tolerance are explicitly deferred (see bottom). General
reference material lives in NOTES.md.

Environment manager (Rust)
--------------------------
- [ ] VM lifecycle in `environments/manager/src/environment.rs`:
      `CreateEnvironment` boots Firecracker from base+task images (via
      `manifest.json`), tracks env_id → VM; implement `Reset` and `Close`.
      All four RPCs are stubs today. `CreateEnvironment` must also return
      `manager_addr` from `GRL_MANAGER_ADVERTISE_ADDR` (direct routing) and
      `RESOURCE_EXHAUSTED` when the node's VM slots are full (admission
      control) — see TODOs in the file.
- [ ] Real `env` executor (`environments/swebench-lite/env`): in-VM tool
      execution (bash, file view/edit) over vsock, with per-call timeouts.
      Currently prints "executor".
- [ ] Add `Evaluate(env_id) → reward` RPC to the proto: run FAIL_TO_PASS /
      PASS_TO_PASS tests in the VM. Bake per-instance test commands into the
      manifest or task image.

Training correctness
--------------------
- [ ] GRPO loss in `trainer.py` (currently a no-op): group-relative
      advantages, KL vs ref model, optimizer step. Include from the start:
      zero-variance group filtering (all-same-reward groups contribute
      nothing) and a deliberate token-level vs sequence-level aggregation
      choice.
- [ ] Wire rewards through: call `Evaluate` after each trajectory and attach
      to `RolloutResult` before the batcher. `reward` is always `None` today.
- [ ] Fix batcher liveness bug: groups whose rollouts straddle a policy
      version never complete. Batch on group completion with a staleness
      bound instead of exact-version matching.
- [ ] Token-In-Token-Out semantics (e.g. prime renderers lib): keep exact
      sampled token ids and logprobs end-to-end so the trainer sees what the
      sampler emitted — no retokenization drift between vLLM and HF.
- [ ] Fix tool-message tokenization: `_tokenize_tool_messages` templates tool
      messages in isolation, re-prepending system/BOS tokens mid-trajectory.
      Render the full message list incrementally and slice. (Subsumed by a
      proper TITO renderer if adopted.)
- [ ] Data feeder: nothing populates `pending_tasks`. Read SWE-bench-Lite
      tasks (parquet/manifest), build system prompt + problem statement +
      tool schemas into `GRPOGroupRequest`s.

Performance
-----------
- [ ] Weight sync (currently `return None` — without it this isn't RL):
      start with object-store state dict + in-place vLLM reload, measure
      sync latency.
- [ ] Sparse weight transfer: send only a delta of the weights from trainer
      to rollout workers (Cursor-style) to cut sync time and keep rollouts
      closer to on-policy.
- [ ] Exploit prefix caching deliberately: the N rollouts in a group share a
      prompt — route them to the same engine using a hashmap in the data submitter
- [ ] Per-call timeouts on gRPC and generation so one hung VM or runaway
      trajectory can't stall a group.

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
- [ ] Job submission for `training.main` (RayJob or `ray job submit`); a
      script is fine — the README's `launcher` doesn't exist yet.
- [ ] Model weights onto GPU nodes (`local_files_only=True` needs them
      present); pick the small model — 1.5B/3B is much cheaper than the
      Qwen2.5-7B default for a first run.

Deferred (scaling / fault tolerance)
------------------------------------
- Multi-node training engine (FSDP/Megatron), multi-engine rollout fleet
- NCCL/RDMA weight broadcast
- Checkpointing + resumption, retries, preemption tolerance
- Off-policy corrections (importance sampling, interruptible generation)
- VM snapshot/restore for fast resets; manager autoscaling / load-aware
  placement (admission control + retry covers the first pass)
