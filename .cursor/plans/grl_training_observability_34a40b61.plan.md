---
name: GRL training observability
overview: Catalog the telemetry needed to monitor GRL RL training runs, then instrument the training code (driver, RolloutWorker, TrainingWorker, environment client) via training.telemetry and the Rust environment manager via a new telemetry.rs, add a ClickHouse-backed trajectory store, and ship a single provisioned Grafana dashboard spanning training through GPU/Ray/manager infra.
todos:
  - id: helpers
    content: Add gauge/observable_gauge/record_duration helpers and OTLP LoggerProvider + log_trajectory() to training/src/training/telemetry.py
    status: completed
  - id: trainer
    content: "Instrument TrainingWorker.train_batch: train_batch/weight_sync spans, loss/kl/pg_loss/grad_norm/clip_fraction/advantage/reward/groups_dropped/policy_version metrics + step durations"
    status: completed
  - id: rollouts
    content: "Instrument RolloutWorker: rollout/generate spans, reward/turns/tokens/staleness/completed/truncated/tool_calls metrics, in_flight observable gauge, weight_reload duration, and log_trajectory() emit"
    status: completed
  - id: env
    content: "Instrument environments.py: rpc duration/retries/errors/infra_errors counters, env.* spans, active-sessions gauge, tool.calls counter"
    status: completed
  - id: pipeline
    content: "Instrument main.py run/batcher_loop: queue-depth observable gauges, partial/ready group gauges, batch emit/size, group assembly duration + timeout counters"
    status: completed
  - id: manager-telemetry-rs
    content: Add environments/manager/src/telemetry.rs (OTLP meter provider + counter/histogram/gauge/observable_gauge/rpc_timer helpers, no-op when endpoint unset) and the opentelemetry/otlp Cargo deps
    status: completed
  - id: manager-instrument
    content: "Instrument the manager: registry phase/active-env/active-vm atomics + observable gauges, admission-rejected counter, RPC timers across all five handlers, VM boot duration/failure, execute/submit/evaluate metrics, catalog gauge; wire init_telemetry in main.rs and OTEL env on the DaemonSet"
    status: completed
  - id: traj-schema
    content: Add grl_trajectories table + mv_grl_trajectories materialized view over gaia_logs to gaia/metrics-pipeline/schema.sql
    status: completed
  - id: dashboard
    content: "Create one cohesive infra/observability/grafana/grl-dashboard.json: Training/Rollouts/vLLM/Pipeline/Environment rows by run_id, plus Manager/Environments, GPU(DCGM) and Ray rows from the landing tables scoped by a derived run time-window, plus a Trajectories table"
    status: completed
  - id: tests
    content: Add tests for telemetry helpers (no-op without endpoint, in-memory reader) and extend rollout/trainer tests to assert key instruments fire
    status: completed
isProject: false
---

# GRL Training Observability

## Context (confirmed)

- `training.telemetry` already wires per-role OTLP export (`init_telemetry` called in [main.py](training/src/training/main.py), [rollouts.py](training/src/training/rollouts.py), [trainer.py](training/src/training/trainer.py)) and exposes `span()`, `counter()`, `histogram()` — but **nothing calls them yet**.
- Resource attrs already set: `service.name=grl-{role}`, `grl.role`, `run.id`.
- Pipeline: workers → in-cluster `grl-collector` → external VM → ClickHouse. Gauges/counters land in `gaia_metrics_landing`/`gaia_metrics_sum_landing` and are promoted by `RunId` into `gaia_metrics`. Histograms land in `gaia_metrics_histogram_landing` (no promotion MV — query directly by `ResourceAttributes['run.id']`). Logs land in `gaia_logs`; traces in `gaia_traces`.
- Grafana uses the `grafana-clickhouse-datasource` (uid `clickhouse`) with a `${run_id}` template var (see `gaia/metrics-pipeline/dashboard.json`).

### Cardinality / keying rules (apply throughout)
- Metric **attributes** stay low-cardinality: `policy_version`, `role`, `done_reason`, `rpc`, `tool`, bucket-style enums. Never put `task_id`/`group_id`/`request_id` on metrics — those belong on spans and trajectory rows only.

### Scraped infra metrics: where they land and how the dashboard reaches them
The collector's `prometheus` receiver ([otelcollector.yaml](infra/modules/resources/chart/templates/otelcollector.yaml)) scrapes three jobs — `ray` (every Ray pod :8080), `vllm` (rollouts pods :vllmMetricsPort), `dcgm` (dcgm-exporter :9400) — and feeds them through the same `metrics` pipeline as our OTLP metrics, then on to ClickHouse. Two consequences drive the dashboard design:
- **No `run.id`.** Scraped series have no `run.id` resource attribute, so the `gaia_metrics` materialized view drops them. They exist **only** in the landing tables (`gaia_metrics_landing` for gauges, `gaia_metrics_sum_landing` for counters/`*_total`, `gaia_metrics_histogram_landing` for histograms). Dashboard panels for these rows must query the landing tables directly.
- **`ServiceName` = job name; relabels become `Attributes`.** The prometheus receiver sets `ServiceName` to the scrape `job_name` (`ray`/`vllm`/`dcgm`), and the `relabel_configs` target labels land in the `Attributes` map: `pod`, `ray_group`, `ray_node_type` (ray job), `pod` (vllm job), `node` (dcgm job). Filter panels on `ServiceName = '<job>'` and break out by these attribute keys.
- **Run-scoping by time window.** Because one run is active per cluster at a time (NOTES.md: one active catalog per cluster), infra panels are scoped to the selected run via a derived `[run_start, run_end]` time window rather than `run_id` (see Part 5 variables).

The **manager** (Part 6) is in the same boat: it is a long-lived, environment-scoped DaemonSet that pushes OTLP directly (the collector's `otlp` receiver comment already names the "Rust env server" as a producer), so its metrics carry `service.name=grl-manager` but no `run.id`. They land in `gaia_metrics_landing`/`_sum_landing` (filter `ServiceName='grl-manager'`) and are dashboarded by the same run time-window.

---

## Part 1 — Telemetry catalog (organized by dashboard row)

Metric names are prefixed `grl.`; type in parens. `pv` = `policy_version` attribute.

### Row: Training (emit from `TrainingWorker.train_batch`)
- `grl.train.batches` (counter) — batches applied.
- `grl.train.tokens` (counter) — response tokens trained on.
- `grl.train.loss` (gauge, attr pv) — DrGRPO loss.
- `grl.train.pg_loss` (gauge) — policy-gradient term mean.
- `grl.train.kl` (gauge) — mean K3 KL vs inference logprobs.
- `grl.train.entropy` (gauge) — mean token entropy (optional).
- `grl.train.grad_norm` (gauge) — pre-step global grad norm.
- `grl.train.clip_fraction` (gauge) — fraction of tokens where ratio was clipped.
- `grl.train.ratio_mean` (gauge) — mean importance ratio.
- `grl.train.advantage` (histogram) — advantage distribution.
- `grl.train.reward` (histogram) — rewards entering the step.
- `grl.train.rollouts_used` (gauge) — valid rollouts after `grpo_valid_rollouts`.
- `grl.train.groups_dropped` (counter, attr reason=all_infra|below_min) — dropped groups.
- `grl.train.policy_version` (gauge) — current policy version.
- `grl.train.step.duration` (histogram, s) — fwd/bwd/step wall time.
- `grl.train.weight_sync.duration` (histogram, s) — `send_weights` + broadcast to all rollout workers.
- Span `train_batch` (attrs batch_id, pv, num_rollouts, loss) → child span `weight_sync`.

### Row: Rollouts (emit from `RolloutWorker`)
- `grl.rollout.completed` (counter, attr done_reason) — completed/infra_error/error/timeout.
- `grl.rollout.reward` (histogram) — per-trajectory reward.
- `grl.rollout.num_turns` (histogram) — assistant turns.
- `grl.rollout.response_tokens` / `grl.rollout.prompt_tokens` (histograms).
- `grl.rollout.duration` (histogram, s) — trajectory wall clock.
- `grl.rollout.policy_staleness` (histogram) — `policy_version_current - policy_version_start`.
- `grl.rollout.truncated` (counter, attr cause=max_turns|gen_timeout|model_len) — incomplete trajectories.
- `grl.rollout.tool_calls` (counter, attr tool) — tool invocations.
- `grl.rollout.generation.duration` (histogram, s) — per-turn `_generate_once`.
- `grl.rollout.weight_reload.duration` (histogram, s) — `_reload_vllm_weights`.
- `grl.rollout.in_flight` (observable gauge) — concurrent trajectories (semaphore in use).
- Span `rollout` (attrs task_id, group_id, rollout_index, pv_start, num_turns, reward, done_reason) with children `env.create`, per-turn `generate`, `env.execute`, `env.evaluate`, `env.teardown`.

### Row: vLLM (scraped, `ServiceName='vllm'`, break out by `Attributes['pod']` — time-windowed)
- Gauges (`gaia_metrics_landing`): `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:gpu_cache_usage_perc`.
- Counters (`gaia_metrics_sum_landing`, rate via `deltaSum`/`runningDifference`): `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`, `vllm:request_success_total`.
- Histograms (`gaia_metrics_histogram_landing`): `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds`.

### Row: Pipeline (emit from [main.py](training/src/training/main.py) loops)
- `grl.pipeline.pending_tasks.depth` / `completed_rollouts.depth` / `train_batches.depth` (observable gauges over `queue.qsize()`).
- `grl.pipeline.groups.partial` / `grl.pipeline.groups.ready` (gauges, set in `batcher_loop`).
- `grl.pipeline.group.assembly.duration` (histogram, s) — first member → group complete.
- `grl.pipeline.group.timeout` (counter) — groups flushed/padded by assembly timeout.
- `grl.pipeline.batch.emitted` (counter, attr reason=full|staleness_flush).
- `grl.pipeline.batch.size` (histogram) — groups per emitted batch.

### Row: Environment (emit from [environments.py](training/src/training/environments.py))
- `grl.env.rpc.duration` (histogram, s; attrs rpc=create|execute|evaluate|teardown|list_tasks, ok).
- `grl.env.rpc.retries` (counter, attr rpc).
- `grl.env.rpc.errors` (counter, attrs rpc, code).
- `grl.env.infra_errors` (counter, attr rpc) — `InfraError` after retries exhausted.
- `grl.env.active` (gauge) — open sessions (create − teardown).
- `grl.env.tool.calls` (counter, attrs tool, is_error).

### Row: GPU (scraped, `ServiceName='dcgm'`, break out by `Attributes['node']` — time-windowed)
- Gauges (`gaia_metrics_landing`): `DCGM_FI_DEV_GPU_UTIL` (%), `DCGM_FI_DEV_FB_USED`/`DCGM_FI_DEV_FB_FREE` (MiB), `DCGM_FI_DEV_POWER_USAGE` (W), `DCGM_FI_DEV_GPU_TEMP` (C), `DCGM_FI_DEV_SM_CLOCK`. Correlating GPU util/mem against training step + rollout throughput in one view is the main "down the stack" payoff.

### Row: Ray (scraped, `ServiceName='ray'`, break out by `Attributes['pod']` / `ray_group` / `ray_node_type` — time-windowed)
- Node gauges (`gaia_metrics_landing`): `ray_node_cpu_utilization`, `ray_node_mem_used`/`ray_node_mem_total`, `ray_node_gpus_utilization`, `ray_object_store_memory` (or `ray_node_object_store_memory_used`).
- Cluster/component gauges: `ray_cluster_active_nodes`, `ray_resources` (CPU/GPU by state), actor/task state counts.
- Counters (`gaia_metrics_sum_landing`): `ray_tasks_*`/`ray_actors_*` totals as needed.
- Note: exact Ray metric names depend on the Ray version's dashboard/metrics exporter; the JSON should be authored against the names present in the landing table for a live run (verify via `SELECT DISTINCT MetricName FROM gaia_metrics_landing WHERE ServiceName='ray'`).

### Row: Manager / Environments (emitted by the Rust manager via telemetry.rs, `ServiceName='grl-manager'`, break out by `Attributes['pod']`/`Attributes['env_id']` — time-windowed)
Source files: [registry.rs](environments/manager/src/registry.rs), [environment.rs](environments/manager/src/environment.rs), [vm/mod.rs](environments/manager/src/vm/mod.rs).
- `grl.manager.envs.active` (observable gauge) — current registry size (`Registry.envs` len) = environments in flight on this pod. **The "current number of active VMs/envs" the request asks for.**
- `grl.manager.envs.by_phase` (observable gauge, attr phase=booting|ready|submitted|evaluated|failed) — env count per `EnvPhase`, so you can see how many are still booting vs ready vs failed.
- `grl.manager.vms.active` (observable gauge) — attached Firecracker VMs (`Registry.vms` len); diverges from `envs.active` while booting or after failure.
- `grl.manager.capacity.max` (gauge) — `max_concurrent` admission cap (from `GRL_MAX_CONCURRENT_ENVS`).
- `grl.manager.capacity.utilization` (gauge) — `envs.active / max_concurrent`.
- `grl.manager.admission.rejected` (counter) — `register_booting` returns `RegistryError::Exhausted` (cap hit; trainer sees RESOURCE_EXHAUSTED and retries).
- `grl.manager.vm.boots` (counter, attr ok=true|false) — boot attempts in `spawn_boot_task`/`vm::boot`.
- `grl.manager.vm.boot.duration` (histogram, s) — `vm::boot` wall time (jailer spawn → socket → config puts → vsock executor handshake).
- `grl.manager.vm.boot.failures` (counter, attr stage optional) — boots that hit `mark_failed`.
- `grl.manager.vm.lifetime` (histogram, s, optional) — boot→teardown lifetime (needs a boot timestamp on the record/VmHandle).
- `grl.manager.vm.stops` (counter) — VMs stopped in `teardown`.
- `grl.manager.rpc.requests` (counter, attrs rpc=list_tasks|create|execute|evaluate|teardown, code=grpc status) and `grl.manager.rpc.duration` (histogram, s, attr rpc) — per-RPC volume/latency/errors across all five handlers.
- `grl.manager.execute.calls` (counter, attrs tool, is_error) and `grl.manager.execute.forward.duration` (histogram, s) — `forward_execute` to the in-VM executor.
- `grl.manager.submit` (counter) — submit-tool invocations (`mark_submitted` ok).
- `grl.manager.evaluate.duration` (histogram, s), `grl.manager.evaluate.reward` (histogram), `grl.manager.evaluate.infra_errors` (counter) — grading via `forward_evaluate`.
- `grl.manager.catalog.tasks` (gauge) — `Catalog.len()` at startup.
- Spans (optional/stretch): one span per RPC; if the Python client injects W3C `traceparent` into gRPC metadata, the manager can continue the trace so a `rollout` span links to manager `create`/`execute`/`evaluate` spans (true up-and-down-the-stack tracing). Deferred unless trace propagation is wired on both sides.

### Row: Evals (derived from the rollout stream — no separate eval harness today)
- `grl.eval.success` (counter, derived: reward ≥ threshold) and reward-by-`task_id` surfaced via the trajectory table. A dedicated holdout-eval loop is out of scope (noted as follow-up).

---

## Part 2 — `training.telemetry` helper additions ([telemetry.py](training/src/training/telemetry.py))

- Add `gauge(name, unit, description)` — cached `meter.create_gauge(...)` (synchronous gauge, like the gaia helper).
- Add `observable_gauge(name, callbacks, unit, description)` — for queue depths / in-flight counts via callbacks (no sampler loop needed).
- Add a `record_duration(histogram_name, **attrs)` context manager (perf-counter timing → `histogram().record`) to keep call sites terse.
- Initialize a **LoggerProvider** in `init_telemetry` (OTLP log exporter from the same `opentelemetry-exporter-otlp-proto-grpc` package + `BatchLogRecordProcessor`) and register it in `_shutdown`.
- Add `log_trajectory(*, task_id, group_id, rollout_index, policy_version_start, policy_version_current, num_turns, reward, done_reason, prompt, response, ...)` — emits one log record per finished rollout: structured fields as log attributes, marker attribute `grl.record="trajectory"`, full rendered prompt/response text in the body (decoded via tokenizer). `run.id` is inherited from the Resource.
- All helpers stay no-op safe when the endpoint is unset.

---

## Part 3 — Instrumentation call sites

- [trainer.py](training/src/training/trainer.py) `train_batch`: wrap in `span("train_batch", ...)`; time fwd/bwd/step; compute and record loss/kl/pg_loss/grad_norm/clip_fraction/ratio_mean/advantage/reward; count groups dropped in `_flatten_rollouts`; set `grl.train.policy_version`; wrap `send_weights`+broadcast in `weight_sync` span + duration. Return small stats from `_compute_loss` (or compute alongside) so metrics don't require a second pass.
- [rollouts.py](training/src/training/rollouts.py) `run_rollout`/`_run_rollout_inner`/`_run_trajectory`: root `rollout` span; time the trajectory; on completion record reward/turns/tokens/staleness/completed{done_reason}/truncated{cause}; per-turn `generate` span + `grl.rollout.generation.duration`; `grl.rollout.tool_calls{tool}` in `_execute_tool`; `_reload_vllm_weights` duration; register `grl.rollout.in_flight` observable gauge over the semaphore; call `log_trajectory(...)` at the end of `_run_rollout_inner`.
- [environments.py](training/src/training/environments.py): instrument `_grpc_retry` (retries/errors/infra_errors counters) and wrap `create`/`execute`/`evaluate`/`teardown`/`list_task_ids` with `record_duration("grl.env.rpc.duration", rpc=...)` + `env.*` spans (as children of the active `rollout` span via context propagation); `grl.env.active` gauge inc/dec around session lifetime; `grl.env.tool.calls{tool,is_error}` in `execute`.
- [main.py](training/src/training/main.py) `run`: register observable gauges for the three queue depths; in `batcher_loop` set `groups.partial`/`groups.ready`, record `group.assembly.duration`, `group.timeout`, `batch.emitted{reason}`, `batch.size`. Tag training/rollout metrics with `policy_version` where available.

Tests: extend [test_rollouts.py](training/tests/test_rollouts.py) and add a `tests/test_telemetry.py` asserting helpers are no-op without an endpoint and that instrumented paths record the expected instruments using an in-memory metric reader / span exporter.

---

## Part 4 — Trajectory storage (new ClickHouse table + writer)

Reuse the existing logs path (no collector change needed — `gaia/metrics-pipeline/otel-collector.yaml` already routes logs → `gaia_logs`). Mirror the gauge/sum promotion pattern with a materialized view, in [gaia/metrics-pipeline/schema.sql](metrics-pipeline/schema.sql):

- New table `grl_trajectories` (typed columns: `RunId LowCardinality(String)`, `TimeUnix DateTime64(9)`, `TaskId`, `GroupId`, `RolloutIndex UInt32`, `PolicyVersionStart`/`Current UInt32`, `NumTurns UInt32`, `Reward Float64`, `DoneReason LowCardinality(String)`, `PromptTokens`/`ResponseTokens UInt32`, `Body String` for full text), `ORDER BY (RunId, TaskId, TimeUnix)`.
- New MV `mv_grl_trajectories` reading `gaia_logs` where `LogAttributes['grl.record'] = 'trajectory'` and `ResourceAttributes['run.id'] != ''`, extracting fields from `LogAttributes`/`ResourceAttributes` and `Body`.
- Writer = `log_trajectory(...)` from Part 2 (driver/worker emits via OTLP logs). No direct ClickHouse connection from the cluster, consistent with current architecture.

---

## Part 5 — Manager service instrumentation (new `telemetry.rs`)

Mirror `training/telemetry.py` on the Rust side so the manager pushes OTLP metrics to the same in-cluster `grl-collector` (which already lists the "Rust env server" as an expected OTLP producer).

### New file `environments/manager/src/telemetry.rs`
- `pub fn init_telemetry(role: &str) -> Option<TelemetryGuard>`: read `OTEL_EXPORTER_OTLP_ENDPOINT`; if unset, return `None` (no-op, matching the Python disabled path). Otherwise build a `SdkMeterProvider` with a `PeriodicReader` over an OTLP/gRPC metric exporter and a `Resource` of `service.name=grl-manager`, `grl.role=manager`, plus `env.id` (`GRL_ENV_ID`) and `pod` (`HOSTNAME`) for per-pod/per-env breakouts. Set it global via `opentelemetry::global::set_meter_provider`. Return a guard whose `Drop`/explicit `shutdown()` force-flushes (call before `main` exits).
- Thin helpers over the global meter: `counter(name)`, `histogram(name)`, `gauge(name)`, and `observable_gauge(name, callback)` — cached/created once, matching the `counter()`/`histogram()`/`gauge()` ergonomics added to telemetry.py.
- A small `rpc_timer(rpc: &str)` helper (RAII) that records `grl.manager.rpc.duration` + increments `grl.manager.rpc.requests{rpc,code}` on drop, set with the resulting `tonic::Code`.

### Cargo deps ([Cargo.toml](environments/manager/Cargo.toml))
Add `opentelemetry` (metrics API), `opentelemetry_sdk` (feature `rt-tokio`), `opentelemetry-otlp` (feature `grpc-tonic`, reusing the existing `tonic`/`prost` stack), and `opentelemetry-semantic-conventions`. Pin to one compatible release line.

### Wiring (no per-request `run.id`; metrics are pod/env-scoped)
- [main.rs](environments/manager/src/main.rs): `let _telemetry = telemetry::init_telemetry("manager");` at startup; record `grl.manager.catalog.tasks = catalog.len()`; keep the guard alive for the server lifetime so the periodic reader flushes.
- [registry.rs](environments/manager/src/registry.rs): maintain plain counters of state for the observable gauges. Simplest design: add `AtomicUsize` fields (active envs, per-phase counts, active vms) updated inside the existing `write()` paths (`register_booting`, `set_ready`, `mark_*`, `attach_vm`, `take_vm`, `remove`); register observable gauges in `init` whose callbacks read these atomics synchronously (avoids async-in-callback against the `RwLock`). Increment `grl.manager.admission.rejected` on the `Exhausted` branch.
- [environment.rs](environments/manager/src/environment.rs): wrap each of the five handlers with `rpc_timer(...)`; in `create_environment` it already maps `Exhausted`→RESOURCE_EXHAUSTED (tie the admission counter here or in registry); in `execute` record `grl.manager.execute.calls{tool,is_error}` + `execute.forward.duration` around `forward_execute`, and `grl.manager.submit`; in `evaluate` record `evaluate.duration`/`evaluate.reward`/`evaluate.infra_errors` around `forward_evaluate`; in `teardown` increment `grl.manager.vm.stops`.
- [vm/mod.rs](environments/manager/src/vm/mod.rs) `boot` (called from `spawn_boot_task`): time the whole boot and record `grl.manager.vm.boot.duration` + `grl.manager.vm.boots{ok}`; on the `Err`/`mark_failed` path increment `grl.manager.vm.boot.failures`.
- Deployment: set `OTEL_EXPORTER_OTLP_ENDPOINT` (and `GRL_ENV_ID`) on the manager DaemonSet via Helm values so it points at `grl-collector` (same endpoint the Python workers use).

### Tests
Rust unit test that `init_telemetry` returns `None` when the endpoint env is unset (no-op safety), and a registry test asserting the active-env/phase atomics track lifecycle transitions (extending the existing `registry.rs` tests).

---

## Part 6 — Provisioned Grafana dashboard (one cohesive view, top to bottom)

Create `infra/observability/grafana/grl-dashboard.json` — a single dashboard on the `grafana-clickhouse-datasource` (uid `clickhouse`) that shows the whole stack for one run: training signal at the top, down through rollouts/vLLM, the async pipeline, the environment, and the GPU/Ray infrastructure underneath, plus a trajectory browser. The infra rows (GPU/Ray/vLLM) sit in the same dashboard and are time-aligned to the same run as the training rows, so a loss stall can be read against GPU util, vLLM queue depth, and Ray memory at a glance.

### Template variables
- `run_id` (query): `SELECT DISTINCT RunId FROM default.gaia_metrics ORDER BY RunId DESC` — the primary selector for all OTLP rows.
- `run_start` (hidden query): `SELECT toString(min(TimeUnix)) FROM default.gaia_metrics WHERE RunId = '${run_id}'`.
- `run_end` (hidden query): `SELECT toString(max(TimeUnix)) FROM default.gaia_metrics WHERE RunId = '${run_id}'`.
- Set the dashboard time range to follow `${run_start}`–`${run_end}` (or instruct users to "zoom to data"); `run_start`/`run_end` bound the infra panels so scraped series are scoped to the selected run despite lacking `run.id`.

### Query patterns
- OTLP gauge/counter rows (Training, Pipeline, Environment): `... FROM default.gaia_metrics WHERE $__timeFilter(TimeUnix) AND RunId = '${run_id}' AND MetricName = '<grl.*>' ...` (mirrors `gaia/metrics-pipeline/dashboard.json`).
- OTLP histogram panels (rollout reward/turns/latency, env rpc p50/p95): query `gaia_metrics_histogram_landing` directly filtered by `ResourceAttributes['run.id'] = '${run_id}'` (no `RunId` promotion MV for histograms); derive quantiles from `BucketCounts`/`ExplicitBounds`.
- Scraped infra rows (GPU/Ray/vLLM): query the landing tables filtered by `ServiceName` + the run window, e.g. GPU util:
```sql
SELECT TimeUnix AS time, Attributes['node'] AS node, Value
FROM default.gaia_metrics_landing
WHERE TimeUnix BETWEEN parseDateTime64BestEffort('${run_start}')
                   AND parseDateTime64BestEffort('${run_end}')
  AND ServiceName = 'dcgm'
  AND MetricName = 'DCGM_FI_DEV_GPU_UTIL'
ORDER BY TimeUnix
```
  Ray uses `ServiceName='ray'` broken out by `Attributes['pod']`/`ray_group`; vLLM uses `ServiceName='vllm'` by `Attributes['pod']`; the manager uses `ServiceName='grl-manager'` by `Attributes['pod']`/`env_id`; counters read from `gaia_metrics_sum_landing` with `runningDifference(Value)` per series for rates; vLLM latency histograms from `gaia_metrics_histogram_landing`.

### Rows (top to bottom)
1. **Training** — loss / kl / pg_loss / grad_norm / reward timeseries; policy_version + latest-loss stats; rollouts_used and groups_dropped.
2. **Rollouts** — completed-by-done_reason, reward / num_turns / response_tokens histograms, policy_staleness, truncations, tool_calls by tool.
3. **vLLM** — requests running/waiting, KV-cache usage, prompt/generation token throughput, TTFT and e2e latency p50/p95 (by pod).
4. **Pipeline** — queue depths (pending/completed/train_batches), partial vs ready groups, batch size + emit reason, group assembly duration, group timeouts.
5. **Environment (client view)** — rpc duration p50/p95 by rpc, error + retry rates, infra_errors, active sessions, tool calls (from the trainer-side `grl.env.*`).
6. **Manager / Environments (server view)** — active envs and active VMs, envs-by-phase (stacked: booting/ready/submitted/evaluated/failed), capacity utilization vs `max_concurrent`, admission-rejected rate, VM boot duration p50/p95 and boot-failure rate, evaluate reward + infra errors, manager rpc latency/error rate — per manager `pod`. Pairs with row 5 to see both ends of each gRPC call.
7. **GPU (DCGM)** — GPU util %, framebuffer used/free, power, temperature, SM clock — per `node`, time-aligned with rows 1-2.
8. **Ray** — node CPU / memory / GPU util, object-store memory, active nodes, actor/task counts — per `pod`/`ray_group`.
9. **Trajectories** — table panel over `grl_trajectories` (TaskId, Reward, NumTurns, DoneReason, tokens) filtered by `RunId='${run_id}'`, with a row-detail / drilldown panel showing `Body` (full rendered prompt + response).

Grafana is not deployed by the `grl` repo today, so the JSON is checked in for import into the existing Grafana that owns the `clickhouse` datasource (the same one `gaia/metrics-pipeline/dashboard.json` targets); wire it into provisioning if/when Grafana is added to `infra/`.