"""Generates grl-dashboard.json. Run once; the JSON is the checked-in artifact.

Kept in-tree so the (large) dashboard can be regenerated/extended consistently
rather than hand-edited. See grl_training_observability plan Part 6.
"""

import json

DS = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}

panels: list[dict] = []
_id = [0]


def nid() -> int:
    _id[0] += 1
    return _id[0]


# ---- layout engine: flow panels left-to-right across a 24-col grid ----
_cur = {"x": 0, "y": 0, "row_h": 0}


def place(w: int, h: int) -> dict:
    if _cur["x"] + w > 24:
        _cur["x"] = 0
        _cur["y"] += _cur["row_h"]
        _cur["row_h"] = 0
    pos = {"x": _cur["x"], "y": _cur["y"], "w": w, "h": h}
    _cur["x"] += w
    _cur["row_h"] = max(_cur["row_h"], h)
    return pos


def row(title: str) -> None:
    if _cur["x"] != 0:
        _cur["y"] += _cur["row_h"]
        _cur["x"] = 0
        _cur["row_h"] = 0
    panels.append(
        {
            "collapsed": False,
            "gridPos": {"x": 0, "y": _cur["y"], "w": 24, "h": 1},
            "id": nid(),
            "title": title,
            "type": "row",
            "panels": [],
        }
    )
    _cur["y"] += 1


def target(sql: str, ref: str = "A", fmt: int = 0) -> dict:
    return {
        "datasource": DS,
        "editorType": "sql",
        "format": fmt,  # 0=time series, 1=table
        "rawSql": sql,
        "refId": ref,
    }


def timeseries(title: str, sql: str, w: int = 12, h: int = 8, unit: str = "short",
               stack: bool = False, fill: int = 10) -> None:
    panels.append({
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "axisBorderShow": False, "axisCenteredZero": False,
                    "axisColorMode": "text", "axisLabel": "", "axisPlacement": "auto",
                    "barAlignment": 0, "drawStyle": "line", "fillOpacity": fill,
                    "gradientMode": "none",
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                    "insertNulls": False, "lineInterpolation": "linear", "lineWidth": 2,
                    "pointSize": 4, "scaleDistribution": {"type": "linear"},
                    "showPoints": "auto", "spanNulls": False,
                    "stacking": {"group": "A", "mode": "normal" if stack else "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "unit": unit,
            },
            "overrides": [],
        },
        "gridPos": place(w, h),
        "id": nid(),
        "options": {
            "legend": {"calcs": ["lastNotNull", "max"], "displayMode": "table",
                       "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [target(sql)],
        "title": title,
        "type": "timeseries",
    })


def stat(title: str, sql: str, w: int = 4, h: int = 4, unit: str = "short") -> None:
    panels.append({
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "unit": unit,
            },
            "overrides": [],
        },
        "gridPos": place(w, h),
        "id": nid(),
        "options": {
            "colorMode": "value", "graphMode": "area", "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showPercentChange": False, "textMode": "auto", "wideLayout": True,
        },
        "targets": [target(sql, fmt=1)],
        "title": title,
        "type": "stat",
    })


def table(title: str, sql: str, w: int = 24, h: int = 10) -> None:
    panels.append({
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
            },
            "overrides": [],
        },
        "gridPos": place(w, h),
        "id": nid(),
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "targets": [target(sql, fmt=1)],
        "title": title,
        "type": "table",
    })


# ---- query builders ----------------------------------------------------------

def q_otlp_multi(names: list[str]) -> str:
    lst = ", ".join(f"'{n}'" for n in names)
    return (
        "SELECT TimeUnix AS time, MetricName AS metric, Value\n"
        "FROM default.grl_metrics\n"
        "WHERE $__timeFilter(TimeUnix) AND RunId = '${run_id}'\n"
        f"  AND MetricName IN ({lst})\n"
        "ORDER BY TimeUnix"
    )


def q_otlp_by_attr(name: str, attr: str) -> str:
    return (
        f"SELECT TimeUnix AS time, Attributes['{attr}'] AS series, Value\n"
        "FROM default.grl_metrics\n"
        "WHERE $__timeFilter(TimeUnix) AND RunId = '${run_id}'\n"
        f"  AND MetricName = '{name}'\n"
        "ORDER BY TimeUnix"
    )


def q_otlp_stat(name: str) -> str:
    return (
        "SELECT argMax(Value, TimeUnix) AS value\n"
        "FROM default.grl_metrics\n"
        "WHERE $__timeFilter(TimeUnix) AND RunId = '${run_id}'\n"
        f"  AND MetricName = '{name}'"
    )


def q_otlp_counter_rate(name: str, attr: str | None = None) -> str:
    """Per-interval increment of a cumulative OTLP counter (window diff)."""
    series = f"Attributes['{attr}']" if attr else "''"
    return (
        "SELECT t AS time, series, greatest(0, v - lagInFrame(v) OVER "
        "(PARTITION BY series ORDER BY t)) AS delta\n"
        "FROM (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        f"         {series} AS series, max(Value) AS v\n"
        "  FROM default.grl_metrics\n"
        "  WHERE $__timeFilter(TimeUnix) AND RunId = '${run_id}'\n"
        f"    AND MetricName = '{name}'\n"
        "  GROUP BY t, series\n"
        ")\n"
        "ORDER BY t"
    )


def _quantile_expr(bc: str, eb: str, q: float) -> str:
    return (
        f"arrayElement({eb}, greatest(1, least(arrayFirstIndex(c -> c >= "
        f"{q} * arraySum({bc}), arrayCumSum({bc})), length({eb}))))"
    )


def q_otlp_hist_quant(name: str) -> str:
    p50 = _quantile_expr("bc", "eb", 0.50)
    p95 = _quantile_expr("bc", "eb", 0.95)
    return (
        "SELECT t AS time,\n"
        f"  {p50} AS p50,\n"
        f"  {p95} AS p95\n"
        "FROM (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        "         sumForEach(BucketCounts) AS bc, any(ExplicitBounds) AS eb\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        "  WHERE $__timeFilter(TimeUnix) AND ResourceAttributes['run.id'] = '${run_id}'\n"
        f"    AND MetricName = '{name}'\n"
        "  GROUP BY t\n"
        ")\n"
        "WHERE arraySum(bc) > 0\nORDER BY t"
    )


def q_otlp_hist_quant_by_attr(name: str, attr: str) -> str:
    """p50/p95 of an OTLP histogram, one series per value of ``attr``.

    Buckets are merged per (interval, attr) with sumForEach, then a quantile is
    estimated per group; the two quantiles are unioned into long form
    (time, series='<attr> p50|p95', value) so the datasource pivots one line per
    attribute value and quantile.
    """
    p50 = _quantile_expr("bc", "eb", 0.50)
    p95 = _quantile_expr("bc", "eb", 0.95)
    return (
        "WITH grouped AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        f"         Attributes['{attr}'] AS k,\n"
        "         sumForEach(BucketCounts) AS bc, any(ExplicitBounds) AS eb\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        "  WHERE $__timeFilter(TimeUnix) AND ResourceAttributes['run.id'] = '${run_id}'\n"
        f"    AND MetricName = '{name}'\n"
        "  GROUP BY t, k\n"
        ")\n"
        "SELECT time, series, value FROM (\n"
        f"  SELECT t AS time, concat(k, ' p50') AS series, {p50} AS value\n"
        "  FROM grouped WHERE arraySum(bc) > 0\n"
        "  UNION ALL\n"
        f"  SELECT t AS time, concat(k, ' p95') AS series, {p95} AS value\n"
        "  FROM grouped WHERE arraySum(bc) > 0\n"
        ")\nORDER BY time"
    )


def q_otlp_hist_avg(name: str) -> str:
    """Mean of an OTLP histogram over time (Sum/Count per interval)."""
    return (
        "SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS time,\n"
        "       sum(Sum) / nullIf(sum(Count), 0) AS mean\n"
        "FROM default.grl_metrics_histogram_landing\n"
        "WHERE $__timeFilter(TimeUnix) AND ResourceAttributes['run.id'] = '${run_id}'\n"
        f"  AND MetricName = '{name}'\n"
        "GROUP BY time\nORDER BY time"
    )


# Scraped infra (no run.id) -> landing tables, scoped to the run time window.
WINDOW = (
    "TimeUnix BETWEEN parseDateTime64BestEffort('${run_start}') "
    "AND parseDateTime64BestEffort('${run_end}')"
)


def q_scraped_gauge(name: str, svc: str, by: str) -> str:
    return (
        f"SELECT TimeUnix AS time, Attributes['{by}'] AS series, Value\n"
        "FROM default.grl_metrics_landing\n"
        f"WHERE {WINDOW}\n"
        f"  AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "ORDER BY TimeUnix"
    )


def q_scraped_by_phase(name: str, svc: str) -> str:
    return q_scraped_gauge(name, svc, "phase")


def q_scraped_counter_rate(name: str, svc: str, by: str | None = None) -> str:
    series = f"Attributes['{by}']" if by else "''"
    return (
        "SELECT t AS time, series, greatest(0, v - lagInFrame(v) OVER "
        "(PARTITION BY series ORDER BY t)) AS delta\n"
        "FROM (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        f"         {series} AS series, max(Value) AS v\n"
        "  FROM default.grl_metrics_sum_landing\n"
        f"  WHERE {WINDOW}\n"
        f"    AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "  GROUP BY t, series\n"
        ")\n"
        "ORDER BY t"
    )


def q_scraped_hist_quant(name: str, svc: str) -> str:
    p50 = _quantile_expr("bc", "eb", 0.50)
    p95 = _quantile_expr("bc", "eb", 0.95)
    return (
        "SELECT t AS time,\n"
        f"  {p50} AS p50,\n"
        f"  {p95} AS p95\n"
        "FROM (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        "         sumForEach(BucketCounts) AS bc, any(ExplicitBounds) AS eb\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        f"  WHERE {WINDOW}\n"
        f"    AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "  GROUP BY t\n"
        ")\n"
        "WHERE arraySum(bc) > 0\nORDER BY t"
    )


def q_scraped_hist_quant_by_attr(name: str, svc: str, attr: str) -> str:
    """Scraped-histogram p50/p95, one series per value of ``attr`` (run window)."""
    p50 = _quantile_expr("bc", "eb", 0.50)
    p95 = _quantile_expr("bc", "eb", 0.95)
    return (
        "WITH grouped AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        f"         Attributes['{attr}'] AS k,\n"
        "         sumForEach(BucketCounts) AS bc, any(ExplicitBounds) AS eb\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        f"  WHERE {WINDOW}\n"
        f"    AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "  GROUP BY t, k\n"
        ")\n"
        "SELECT time, series, value FROM (\n"
        f"  SELECT t AS time, concat(k, ' p50') AS series, {p50} AS value\n"
        "  FROM grouped WHERE arraySum(bc) > 0\n"
        "  UNION ALL\n"
        f"  SELECT t AS time, concat(k, ' p95') AS series, {p95} AS value\n"
        "  FROM grouped WHERE arraySum(bc) > 0\n"
        ")\nORDER BY time"
    )


# ============================ ROWS ============================================

# 1. Training -----------------------------------------------------------------
row("Training")
timeseries("Loss / PG loss / KL",
           q_otlp_multi(["grl.train.loss", "grl.train.pg_loss", "grl.train.kl"]))
timeseries("Grad norm / clip fraction / ratio mean",
           q_otlp_multi(["grl.train.grad_norm", "grl.train.clip_fraction",
                         "grl.train.ratio_mean"]))
timeseries("Reward entering step (p50/p95)", q_otlp_hist_quant("grl.train.reward"), w=8)
timeseries("Advantage (p50/p95)", q_otlp_hist_quant("grl.train.advantage"), w=8)
stat("Policy version", q_otlp_stat("grl.train.policy_version"), w=4)
stat("Rollouts used", q_otlp_stat("grl.train.rollouts_used"), w=4)
timeseries("Groups dropped / 30s (by reason)",
           q_otlp_counter_rate("grl.train.groups_dropped", "reason"), w=12)
timeseries("Step & weight-sync duration (p50/p95)",
           q_otlp_hist_quant("grl.train.step.duration"), w=6, unit="s")
timeseries("Weight sync duration (p50/p95)",
           q_otlp_hist_quant("grl.train.weight_sync.duration"), w=6, unit="s")

# 2. Rollouts -----------------------------------------------------------------
row("Rollouts")
timeseries("Completed / 30s (by done_reason)",
           q_otlp_counter_rate("grl.rollout.completed", "done_reason"), w=8)
timeseries("Truncated / 30s (by cause)",
           q_otlp_counter_rate("grl.rollout.truncated", "cause"), w=8)
timeseries("Tool calls / 30s (by tool)",
           q_otlp_counter_rate("grl.rollout.tool_calls", "tool"), w=8)
timeseries("Reward (p50/p95)", q_otlp_hist_quant("grl.rollout.reward"), w=8)
timeseries("Num turns (p50/p95)", q_otlp_hist_quant("grl.rollout.num_turns"), w=8)
timeseries("Policy staleness (p50/p95)",
           q_otlp_hist_quant("grl.rollout.policy_staleness"), w=8)
timeseries("Response / prompt tokens (mean)",
           q_otlp_hist_avg("grl.rollout.response_tokens"), w=8)
timeseries("Trajectory duration (p50/p95)",
           q_otlp_hist_quant("grl.rollout.duration"), w=8, unit="s")
timeseries("In-flight trajectories", q_otlp_by_attr("grl.rollout.in_flight", "grl.role"),
           w=8)

# 3. vLLM (scraped) -----------------------------------------------------------
row("vLLM (scraped: ServiceName='vllm', by pod)")
timeseries("Requests running / waiting",
           q_scraped_gauge("vllm:num_requests_running", "vllm", "pod"), w=8)
timeseries("KV cache usage %",
           q_scraped_gauge("vllm:kv_cache_usage_perc", "vllm", "pod"), w=8, unit="percentunit")
timeseries("Requests waiting",
           q_scraped_gauge("vllm:num_requests_waiting", "vllm", "pod"), w=8)
timeseries("Prompt tokens / 30s",
           q_scraped_counter_rate("vllm:prompt_tokens_total", "vllm", "pod"), w=8)
timeseries("Generation tokens / 30s",
           q_scraped_counter_rate("vllm:generation_tokens_total", "vllm", "pod"), w=8)
timeseries("Time to first token (p50/p95)",
           q_scraped_hist_quant("vllm:time_to_first_token_seconds", "vllm"), w=8, unit="s")
timeseries("E2E request latency (p50/p95)",
           q_scraped_hist_quant("vllm:e2e_request_latency_seconds", "vllm"), w=12, unit="s")

# 4. Pipeline -----------------------------------------------------------------
row("Pipeline")
timeseries("Queue depths",
           q_otlp_multi(["grl.pipeline.pending_tasks.depth",
                         "grl.pipeline.completed_rollouts.depth",
                         "grl.pipeline.train_batches.depth"]), w=12)
timeseries("Groups partial vs ready",
           q_otlp_multi(["grl.pipeline.groups.partial", "grl.pipeline.groups.ready"]), w=12)
timeseries("Batches emitted / 30s (by reason)",
           q_otlp_counter_rate("grl.pipeline.batch.emitted", "reason"), w=8)
timeseries("Batch size (p50/p95)", q_otlp_hist_quant("grl.pipeline.batch.size"), w=8)
timeseries("Group assembly duration (p50/p95)",
           q_otlp_hist_quant("grl.pipeline.group.assembly.duration"), w=8, unit="s")
timeseries("Group assembly timeouts / 30s",
           q_otlp_counter_rate("grl.pipeline.group.timeout"), w=12)

# 5. Environment (client view) ------------------------------------------------
row("Environment (trainer-side gRPC client)")
timeseries("RPC duration p50/p95 (by rpc)",
           q_otlp_hist_quant_by_attr("grl.env.rpc.duration", "rpc"), w=12, unit="s")
timeseries("Active sessions", q_otlp_by_attr("grl.env.active", "grl.role"), w=12)
timeseries("RPC errors / 30s (by rpc)",
           q_otlp_counter_rate("grl.env.rpc.errors", "rpc"), w=8)
timeseries("RPC retries / 30s (by rpc)",
           q_otlp_counter_rate("grl.env.rpc.retries", "rpc"), w=8)
timeseries("Infra errors / 30s (by rpc)",
           q_otlp_counter_rate("grl.env.infra_errors", "rpc"), w=8)
timeseries("Tool calls / 30s (by tool)",
           q_otlp_counter_rate("grl.env.tool.calls", "tool"), w=12)

# 6. Manager / Environments (server view) -------------------------------------
row("Manager / Environments (scraped: ServiceName='grl-manager', by pod)")
timeseries("Active envs (by pod)",
           q_scraped_gauge("grl.manager.envs.active", "grl-manager", "pod"), w=8)
timeseries("Active VMs (by pod)",
           q_scraped_gauge("grl.manager.vms.active", "grl-manager", "pod"), w=8)
timeseries("Envs by phase", q_scraped_by_phase("grl.manager.envs.by_phase", "grl-manager"),
           w=8, stack=True)
timeseries("Capacity utilization",
           q_scraped_gauge("grl.manager.capacity.utilization", "grl-manager", "pod"),
           w=8, unit="percentunit")
timeseries("Admission rejected / 30s",
           q_scraped_counter_rate("grl.manager.admission.rejected", "grl-manager"), w=8)
timeseries("VM boots / 30s (by ok)",
           q_scraped_counter_rate("grl.manager.vm.boots", "grl-manager", "ok"), w=8)
timeseries("VM boot duration (p50/p95)",
           q_scraped_hist_quant("grl.manager.vm.boot.duration", "grl-manager"), w=8, unit="s")
timeseries("VM boot failures / 30s",
           q_scraped_counter_rate("grl.manager.vm.boot.failures", "grl-manager"), w=8)
timeseries("Evaluate reward (p50/p95)",
           q_scraped_hist_quant("grl.manager.evaluate.reward", "grl-manager"), w=8)
timeseries("Manager RPC duration p50/p95 (by rpc)",
           q_scraped_hist_quant_by_attr("grl.manager.rpc.duration", "grl-manager", "rpc"),
           w=8, unit="s")
timeseries("Manager RPC requests / 30s (by rpc)",
           q_scraped_counter_rate("grl.manager.rpc.requests", "grl-manager", "rpc"), w=8)
timeseries("Evaluate infra errors / 30s",
           q_scraped_counter_rate("grl.manager.evaluate.infra_errors", "grl-manager"), w=8)

# 7. GPU (DCGM, scraped) ------------------------------------------------------
row("GPU (scraped: ServiceName='dcgm', by node)")
timeseries("GPU utilization %",
           q_scraped_gauge("DCGM_FI_DEV_GPU_UTIL", "dcgm", "node"), w=8, unit="percent")
timeseries("Framebuffer used (MiB)",
           q_scraped_gauge("DCGM_FI_DEV_FB_USED", "dcgm", "node"), w=8, unit="decmbytes")
timeseries("Power usage (W)",
           q_scraped_gauge("DCGM_FI_DEV_POWER_USAGE", "dcgm", "node"), w=8, unit="watt")
timeseries("GPU temperature (C)",
           q_scraped_gauge("DCGM_FI_DEV_GPU_TEMP", "dcgm", "node"), w=8, unit="celsius")
timeseries("SM clock (MHz)",
           q_scraped_gauge("DCGM_FI_DEV_SM_CLOCK", "dcgm", "node"), w=8, unit="rotmhz")
timeseries("Framebuffer free (MiB)",
           q_scraped_gauge("DCGM_FI_DEV_FB_FREE", "dcgm", "node"), w=8, unit="decmbytes")

# 8. Ray (scraped) ------------------------------------------------------------
row("Ray (scraped: ServiceName='ray', by pod / ray_group)")
timeseries("Node CPU utilization",
           q_scraped_gauge("ray_node_cpu_utilization", "ray", "pod"), w=8, unit="percent")
timeseries("Node memory used",
           q_scraped_gauge("ray_node_mem_used", "ray", "pod"), w=8, unit="bytes")
timeseries("Node GPU utilization",
           q_scraped_gauge("ray_node_gpus_utilization", "ray", "pod"), w=8, unit="percent")
timeseries("Object store memory",
           q_scraped_gauge("ray_object_store_memory", "ray", "pod"), w=8, unit="bytes")
timeseries("Cluster active nodes",
           q_scraped_gauge("ray_cluster_active_nodes", "ray", "ray_node_type"), w=8)
timeseries("Resources (by ray_group)",
           q_scraped_gauge("ray_resources", "ray", "ray_group"), w=8)

# 9. Trajectories -------------------------------------------------------------
row("Trajectories")
table(
    "Recent trajectories",
    "SELECT TimeUnix AS time, TaskId, GroupId, RolloutIndex, Reward, NumTurns,\n"
    "       DoneReason, PromptTokens, ResponseTokens, PolicyVersionStart,\n"
    "       PolicyVersionCurrent\n"
    "FROM default.grl_trajectories\n"
    "WHERE RunId = '${run_id}'\n"
    "ORDER BY TimeUnix DESC\nLIMIT 500",
    w=24, h=12,
)
table(
    "Trajectory detail (full prompt + response in Body)",
    "SELECT TimeUnix AS time, TaskId, Reward, DoneReason, Body\n"
    "FROM default.grl_trajectories\n"
    "WHERE RunId = '${run_id}'\n"
    "ORDER BY TimeUnix DESC\nLIMIT 50",
    w=24, h=12,
)


# ============================ DASHBOARD =======================================
dashboard = {
    "annotations": {"list": [{
        "builtIn": 1,
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard",
    }]},
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,  # shared crosshair so all rows line up in time
    "id": None,
    "links": [],
    "liveNow": False,
    "panels": panels,
    "refresh": "30s",
    "schemaVersion": 39,
    "tags": ["grl", "training", "rl", "clickhouse"],
    "templating": {"list": [
        {
            "current": {}, "datasource": DS,
            "definition": "SELECT DISTINCT RunId FROM default.grl_metrics WHERE RunId != '' ORDER BY RunId DESC",
            "hide": 0, "includeAll": False, "label": "Run", "multi": False,
            "name": "run_id", "options": [],
            "query": "SELECT DISTINCT RunId FROM default.grl_metrics WHERE RunId != '' ORDER BY RunId DESC",
            "refresh": 1, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query",
        },
        {
            "current": {}, "datasource": DS,
            "definition": "SELECT toString(min(TimeUnix)) FROM default.grl_metrics WHERE RunId = '${run_id}'",
            "hide": 2, "includeAll": False, "label": "Run start", "multi": False,
            "name": "run_start", "options": [],
            "query": "SELECT toString(min(TimeUnix)) FROM default.grl_metrics WHERE RunId = '${run_id}'",
            "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 0, "type": "query",
        },
        {
            "current": {}, "datasource": DS,
            "definition": "SELECT toString(max(TimeUnix)) FROM default.grl_metrics WHERE RunId = '${run_id}'",
            "hide": 2, "includeAll": False, "label": "Run end", "multi": False,
            "name": "run_end", "options": [],
            "query": "SELECT toString(max(TimeUnix)) FROM default.grl_metrics WHERE RunId = '${run_id}'",
            "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 0, "type": "query",
        },
    ]},
    "time": {"from": "now-6h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "GRL Training Observability",
    "uid": "grl-training-observability",
    "version": 1,
    "weekStart": "",
}

with open("grl-dashboard.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print(f"wrote grl-dashboard.json with {len(panels)} panels")
