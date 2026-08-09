"""Generates grl-dashboard.json. Run once; the JSON is the checked-in artifact.

Kept in-tree so the (large) dashboard can be regenerated/extended consistently
rather than hand-edited. See grl_training_observability plan Part 6.
"""

from __future__ import annotations

import json

DS = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse"}

panels: list[dict] = []
_id = [0]


def nid() -> int:
    _id[0] += 1
    return _id[0]


# ---- layout engine: three uniform panels across a 24-col grid ----
#
# Keep every dashboard panel at 8x8.  ``row()`` advances past the current
# panel height, deliberately preserving blank cells in a partially filled row.
_PANEL_W = 8
_PANEL_H = 8
_cur = {"x": 0, "y": 0, "row_h": 0}


def place(_w: int, _h: int) -> dict:
    if _cur["x"] + _PANEL_W > 24:
        _cur["x"] = 0
        _cur["y"] += _cur["row_h"]
        _cur["row_h"] = 0
    pos = {"x": _cur["x"], "y": _cur["y"], "w": _PANEL_W, "h": _PANEL_H}
    _cur["x"] += _PANEL_W
    _cur["row_h"] = max(_cur["row_h"], _PANEL_H)
    return pos


def place_full(h: int) -> dict:
    """Place an intentional full-width exception on its own row."""
    if _cur["x"] != 0:
        _cur["y"] += _cur["row_h"]
        _cur["x"] = 0
        _cur["row_h"] = 0
    pos = {"x": 0, "y": _cur["y"], "w": 24, "h": h}
    _cur["y"] += h
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


def custom_panel(
    plugin_type: str,
    title: str,
    sql: str,
    options: dict | None = None,
    w: int = 24,
    h: int = 10,
    full_width: bool = False,
) -> None:
    panels.append({
        "datasource": DS,
        "gridPos": place_full(h) if full_width else place(w, h),
        "id": nid(),
        "options": options or {},
        "targets": [target(sql, fmt=1)],
        "title": title,
        "type": plugin_type,
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


def q_scraped_pods_by_attr(metric: str, service: str, attr: str) -> str:
    """Distinct scraped pod count, grouped by a resource attribute."""
    series = (
        f"if(ResourceAttributes['{attr}'] != '', ResourceAttributes['{attr}'], "
        f"Attributes['{attr}'])"
    )
    pod = "if(ResourceAttributes['pod'] != '', ResourceAttributes['pod'], Attributes['pod'])"
    return (
        "SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS time,\n"
        f"       {series} AS series,\n"
        f"       countDistinct({pod}) AS value\n"
        "FROM default.grl_metrics_landing\n"
        f"WHERE {WINDOW}\n"
        f"  AND ServiceName = '{service}' AND MetricName = '{metric}'\n"
        "GROUP BY time, series\nORDER BY time"
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


def q_otlp_hist_avg_multi(names: list[str]) -> str:
    """Mean of several OTLP histograms, one named series per metric."""
    lst = ", ".join(f"'{name}'" for name in names)
    return (
        "SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS time,\n"
        "       MetricName AS metric,\n"
        "       sum(Sum) / nullIf(sum(Count), 0) AS mean\n"
        "FROM default.grl_metrics_histogram_landing\n"
        "WHERE $__timeFilter(TimeUnix) AND ResourceAttributes['run.id'] = '${run_id}'\n"
        f"  AND MetricName IN ({lst})\n"
        "GROUP BY time, MetricName\nORDER BY time"
    )


# Scraped infra (no run.id) -> landing tables, scoped to the run time window.
WINDOW = (
    "TimeUnix BETWEEN parseDateTime64BestEffort('${run_start}') "
    "AND parseDateTime64BestEffort('${run_end}')"
)


def q_scraped_scope(service: str) -> str:
    """Manager is OTLP-pushed and has a run resource; scraped infra uses time."""
    if service == "grl-manager":
        return "$__timeFilter(TimeUnix) AND ResourceAttributes['run.id'] = '${run_id}'"
    return WINDOW


def q_scraped_gauge(name: str, svc: str, by: str) -> str:
    # Identity labels (pod/node) are often resource attrs for OTLP pushers
    # (e.g. grl-manager) and datapoint attrs for Prometheus scrapes (dcgm/ray).
    # Prefer resource, fall back to datapoint so both layouts work.
    series = (
        f"if(ResourceAttributes['{by}'] != '', "
        f"ResourceAttributes['{by}'], Attributes['{by}'])"
    )
    return (
        f"SELECT TimeUnix AS time, {series} AS series, Value\n"
        "FROM default.grl_metrics_landing\n"
        f"WHERE {q_scraped_scope(svc)}\n"
        f"  AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "ORDER BY TimeUnix"
    )


def q_dcgm_gpu_gauge(name: str) -> str:
    """DCGM device metrics labelled with the owning Ray worker group.

    DCGM's DaemonSet scrape carries its Kubernetes node plus a GPU index and
    UUID. Ray's workload-pod scrape carries both that node and its worker-group
    role. Joining on the normalized node and a 30-second bucket associates each
    device with its dedicated Ray node pool; an unassigned device is shown
    explicitly.
    """
    ray_node = "if(ResourceAttributes['node'] != '', ResourceAttributes['node'], Attributes['node'])"
    ray_role = "if(ResourceAttributes['ray_group'] != '', ResourceAttributes['ray_group'], Attributes['ray_group'])"
    dcgm_node = "if(d.ResourceAttributes['node'] != '', d.ResourceAttributes['node'], d.Attributes['node'])"
    return (
        "WITH ray_roles AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS bucket,\n"
        f"         {ray_node} AS node, any({ray_role}) AS role\n"
        "  FROM default.grl_metrics_landing\n"
        f"  WHERE {WINDOW}\n"
        "    AND ServiceName = 'ray' AND MetricName = 'ray_node_cpu_utilization'\n"
        f"    AND {ray_node} != ''\n"
        "  GROUP BY bucket, node\n"
        ")\n"
        "SELECT d.TimeUnix AS time,\n"
        "       concat(\n"
        "         ifNull(nullIf(r.role, ''), 'unassigned'), ' / ',\n"
        f"         {dcgm_node}, ' / GPU ',\n"
        "         if(d.Attributes['gpu'] != '', d.Attributes['gpu'], 'unknown'),\n"
        "         if(d.Attributes['UUID'] != '', concat(' (', d.Attributes['UUID'], ')'), '')\n"
        "       ) AS series,\n"
        "       d.Value\n"
        "FROM default.grl_metrics_landing AS d\n"
        "LEFT JOIN ray_roles AS r\n"
        f"  ON {dcgm_node} = r.node\n"
        " AND toStartOfInterval(d.TimeUnix, INTERVAL 30 SECOND) = r.bucket\n"
        f"WHERE d.TimeUnix BETWEEN parseDateTime64BestEffort('${{run_start}}') "
        "AND parseDateTime64BestEffort('${run_end}')\n"
        f"  AND d.ServiceName = 'dcgm' AND d.MetricName = '{name}'\n"
        "ORDER BY d.TimeUnix"
    )


def q_node_exporter_rate(name: str | tuple[str, ...]) -> str:
    """Per-node counter rate labelled with the Ray workload role when known."""
    ray_node = "if(ResourceAttributes['node'] != '', ResourceAttributes['node'], Attributes['node'])"
    ray_role = "if(ResourceAttributes['ray_group'] != '', ResourceAttributes['ray_group'], Attributes['ray_group'])"
    node = "if(n.ResourceAttributes['node'] != '', n.ResourceAttributes['node'], n.Attributes['node'])"
    device = "if(n.Attributes['device'] != '', n.Attributes['device'], '')"
    names = (
        f"n.MetricName = '{name}'"
        if isinstance(name, str)
        else "n.MetricName IN (" + ", ".join(repr(metric) for metric in name) + ")"
    )
    return (
        "WITH ray_roles AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS bucket,\n"
        f"         {ray_node} AS node, any({ray_role}) AS role\n"
        "  FROM default.grl_metrics_landing\n"
        f"  WHERE {WINDOW}\n"
        "    AND ServiceName = 'ray' AND MetricName = 'ray_node_cpu_utilization'\n"
        f"    AND {ray_node} != ''\n"
        "  GROUP BY bucket, node\n"
        "), samples AS (\n"
        "  SELECT toStartOfInterval(n.TimeUnix, INTERVAL 30 SECOND) AS bucket,\n"
        f"         {node} AS node, {device} AS device, n.MetricName AS metric, max(n.Value) AS value\n"
        "  FROM default.grl_metrics_sum_landing AS n\n"
        f"  WHERE {WINDOW}\n"
        f"    AND n.ServiceName = 'node-exporter' AND {names}\n"
        f"    AND {node} != ''\n"
        "  GROUP BY bucket, node, device, metric\n"
        "), rates AS (\n"
        "  SELECT bucket, node, device, metric,\n"
        "         greatest(0, value - lagInFrame(value) OVER "
        "(PARTITION BY node, device, metric ORDER BY bucket)) / 30 AS value\n"
        "  FROM samples\n"
        ")\n"
        "SELECT r.bucket AS time,\n"
        "       concat(ifNull(nullIf(rr.role, ''), 'unassigned'), ' / ', r.node,\n"
        "              if(r.metric = '', '', concat(' / ', r.metric))) AS series,\n"
        "       sum(r.value) AS value\n"
        "FROM rates AS r\n"
        "LEFT JOIN ray_roles AS rr ON r.node = rr.node AND r.bucket = rr.bucket\n"
        "GROUP BY time, series\n"
        "ORDER BY time"
    )


def q_nccl_payload_throughput() -> str:
    """Payload bytes divided by sender-only NCCL transfer time, per 30 seconds."""
    return (
        "WITH payload AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t, sum(Sum) AS bytes\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        f"  WHERE {WINDOW}\n"
        "    AND ServiceName = 'grl-training'\n"
        "    AND MetricName = 'grl.train.weight_sync.payload_bytes'\n"
        "    AND Attributes['backend'] = 'nccl'\n"
        "  GROUP BY t\n"
        "), transfer AS (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t, sum(Sum) AS seconds\n"
        "  FROM default.grl_metrics_histogram_landing\n"
        f"  WHERE {WINDOW}\n"
        "    AND ServiceName = 'grl-training'\n"
        "    AND MetricName = 'grl.train.weight_sync.transfer.duration'\n"
        "    AND Attributes['backend'] = 'nccl'\n"
        "  GROUP BY t\n"
        ")\n"
        "SELECT payload.t AS time, payload.bytes / nullIf(transfer.seconds, 0) AS value\n"
        "FROM payload INNER JOIN transfer ON payload.t = transfer.t\n"
        "ORDER BY time"
    )


def q_scraped_by_phase(name: str, svc: str) -> str:
    return q_scraped_gauge(name, svc, "phase")


def q_scraped_counter_rate(name: str, svc: str, by: str | tuple[str, ...] | None = None) -> str:
    # Counters from manager are emitted independently by every DaemonSet pod.
    # Diff each pod first, then sum, otherwise a changing max across pods can
    # yield under-counts or artificial spikes.
    if svc == "grl-manager":
        def attr_value(attr: str) -> str:
            return f"if(ResourceAttributes['{attr}'] != '', ResourceAttributes['{attr}'], Attributes['{attr}'])"
        series = (
            "concat(" + ", ' / ', ".join(attr_value(attr) for attr in by) + ")"
            if isinstance(by, tuple)
            else attr_value(by) if by else "''"
        )
        pod = "if(ResourceAttributes['pod'] != '', ResourceAttributes['pod'], Attributes['pod'])"
        return (
            "SELECT t AS time, series, sum(delta) AS delta\n"
            "FROM (\n"
            "  SELECT t, series, greatest(0, v - lagInFrame(v) OVER "
            "(PARTITION BY pod, series ORDER BY t)) AS delta\n"
            "  FROM (\n"
            "    SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
            f"           {series} AS series, {pod} AS pod, max(Value) AS v\n"
            "    FROM default.grl_metrics_sum_landing\n"
            f"    WHERE {q_scraped_scope(svc)}\n"
            f"      AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
            "    GROUP BY t, series, pod\n"
            "  )\n"
            ")\nGROUP BY t, series\nORDER BY t"
        )
    series = f"Attributes['{by}']" if by else "''"
    return (
        "SELECT t AS time, series, greatest(0, v - lagInFrame(v) OVER "
        "(PARTITION BY series ORDER BY t)) AS delta\n"
        "FROM (\n"
        "  SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS t,\n"
        f"         {series} AS series, max(Value) AS v\n"
        "  FROM default.grl_metrics_sum_landing\n"
        f"  WHERE {q_scraped_scope(svc)}\n"
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


def q_scraped_hist_mean_max(name: str, svc: str) -> str:
    """Exact histogram mean plus maximum, avoiding bucket-derived quantiles."""
    return (
        "SELECT toStartOfInterval(TimeUnix, INTERVAL 30 SECOND) AS time,\n"
        "       sum(Sum) / nullIf(sum(Count), 0) AS mean,\n"
        "       max(Max) AS max\n"
        "FROM default.grl_metrics_histogram_landing\n"
        f"WHERE {q_scraped_scope(svc)}\n"
        f"  AND ServiceName = '{svc}' AND MetricName = '{name}'\n"
        "GROUP BY time\n"
        "HAVING sum(Count) > 0\n"
        "ORDER BY time"
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
        f"  WHERE {q_scraped_scope(svc)}\n"
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
stat("Completed policy updates", q_otlp_stat("grl.train.policy_version"), w=4, h=8)
timeseries("Mean training reward", q_otlp_hist_avg("grl.train.reward"), w=8)
timeseries("Policy stability: KL / entropy",
           q_otlp_multi(["grl.train.kl", "grl.train.entropy"]), w=8)
timeseries("Policy update: ratio / clipped fraction",
           q_otlp_multi(["grl.train.ratio_mean", "grl.train.clip_fraction"]), w=6)
timeseries("Time between completed policy updates",
           q_otlp_multi(["grl.train.policy_update.interval"]), w=8, unit="s")
timeseries("Mean policy staleness (update steps)",
           q_otlp_hist_avg("grl.rollout.policy_staleness"), w=6)
timeseries("Optimization: loss / policy-gradient loss",
           q_otlp_multi(["grl.train.loss", "grl.train.pg_loss"]), w=6)
timeseries("Optimization health: gradient norm",
           q_otlp_multi(["grl.train.grad_norm"]), w=6)
timeseries("Training response tokens / 30s",
           q_otlp_counter_rate("grl.train.tokens"), w=8)
timeseries("Mean training-step / weight-sync duration",
           q_otlp_hist_avg_multi(["grl.train.step.duration",
                                  "grl.train.weight_sync.duration"]), w=8, unit="s")
timeseries("NCCL payload throughput",
           q_nccl_payload_throughput(), w=8, unit="Bps")
timeseries("NCCL sender / end-to-end weight-sync duration",
           q_otlp_hist_avg_multi(["grl.train.weight_sync.transfer.duration",
                                  "grl.train.weight_sync.duration"]), w=8, unit="s")
timeseries("Groups dropped / 30s (by reason)",
           q_otlp_counter_rate("grl.train.groups_dropped", "reason"), w=8)

# 2. Rollouts -----------------------------------------------------------------
row("Rollouts")
timeseries("Completed / 30s (by done_reason)",
           q_otlp_counter_rate("grl.rollout.completed", "done_reason"), w=8)
timeseries("Truncated / 30s (by cause)",
           q_otlp_counter_rate("grl.rollout.truncated", "cause"), w=8)
timeseries("Tool calls / 30s (by tool)",
           q_otlp_counter_rate("grl.rollout.tool_calls", "tool"), w=8)
timeseries("Num turns (mean)", q_otlp_hist_avg("grl.rollout.num_turns"), w=8)
timeseries("Response / prompt tokens (mean)",
           q_otlp_hist_avg("grl.rollout.response_tokens"), w=8)
timeseries("Trajectory duration (mean)",
           q_otlp_hist_avg("grl.rollout.duration"), w=8, unit="s")
timeseries("In-flight trajectories", q_otlp_by_attr("grl.rollout.in_flight", "grl.role"),
           w=8)

# 3. vLLM (scraped) -----------------------------------------------------------
row("vLLM")
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
timeseries("Group assembly duration (mean)",
           q_otlp_hist_avg("grl.pipeline.group.assembly.duration"), w=8, unit="s")
timeseries("Group assembly timeouts / 30s",
           q_otlp_counter_rate("grl.pipeline.group.timeout"), w=12)

# 5. Manager / Environment ----------------------------------------------------
row("Manager / Environment")
# Capacity / lifecycle
timeseries("Active envs (by pod)",
           q_scraped_gauge("grl.manager.envs.active", "grl-manager", "pod"), w=8)
timeseries("Active VMs (by pod)",
           q_scraped_gauge("grl.manager.vms.active", "grl-manager", "pod"), w=8)
timeseries("Envs by phase", q_scraped_by_phase("grl.manager.envs.by_phase", "grl-manager"),
           w=8, stack=True)
timeseries("Capacity utilization",
           q_scraped_gauge("grl.manager.capacity.utilization", "grl-manager", "pod"),
           w=8, unit="percentunit")
timeseries("Catalog tasks", q_scraped_gauge("grl.manager.catalog.tasks", "grl-manager", "pod"), w=8)
# Admission / boot
timeseries("Admission rejected / 30s",
           q_scraped_counter_rate("grl.manager.admission.rejected", "grl-manager"), w=8)
timeseries("VM boots / 30s (by ok)",
           q_scraped_counter_rate("grl.manager.vm.boots", "grl-manager", "ok"), w=8)
timeseries("VM boot duration (mean/max)",
           q_scraped_hist_mean_max("grl.manager.vm.boot.duration", "grl-manager"), w=8, unit="s")
# RPC path: client next to manager handler data.
timeseries("Client RPC duration p50/p95 (by rpc)",
           q_otlp_hist_quant_by_attr("grl.env.rpc.duration", "rpc"), w=8, unit="s")
timeseries("Client RPC retries (by rpc)",
           q_otlp_counter_rate("grl.env.rpc.retries", "rpc"), w=8)
timeseries("Client RPC errors (by rpc)",
           q_otlp_counter_rate("grl.env.rpc.errors", "rpc"), w=8)
timeseries("Manager RPC duration p50/p95 (by rpc)",
           q_scraped_hist_quant_by_attr("grl.manager.rpc.duration", "grl-manager", "rpc"),
           w=8, unit="s")
timeseries("Manager request status / 30s (by rpc/code)",
           q_scraped_counter_rate("grl.manager.rpc.requests", "grl-manager", ("rpc", "code")), w=8)
# Execution / evaluation
timeseries("Client tool calls / 30s (by tool)",
           q_otlp_counter_rate("grl.env.tool.calls", "tool"), w=8)
timeseries("Manager execute calls / 30s (by tool)",
           q_scraped_counter_rate("grl.manager.execute.calls", "grl-manager", "tool"), w=8)
timeseries("Submissions / 30s",
           q_scraped_counter_rate("grl.manager.submit", "grl-manager"), w=8)
timeseries("Execute latency (mean/max)",
           q_scraped_hist_mean_max("grl.manager.execute.forward.duration", "grl-manager"), w=8, unit="s")
timeseries("Evaluation duration (mean/max)",
           q_scraped_hist_mean_max("grl.manager.evaluate.duration", "grl-manager"), w=8, unit="s")
timeseries("Evaluation reward (mean/max)",
           q_scraped_hist_mean_max("grl.manager.evaluate.reward", "grl-manager"), w=8)
timeseries("Evaluate infra errors / 30s",
           q_scraped_counter_rate("grl.manager.evaluate.infra_errors", "grl-manager"), w=8)
 # Snapshot panels intentionally remain visible when snapshots are disabled.
timeseries("Snapshot cache results / 30s (by result)",
           q_scraped_counter_rate("grl.manager.snapshot.cache", "grl-manager", "result"), w=8)
timeseries("Snapshot builds / 30s",
           q_scraped_counter_rate("grl.manager.snapshot.builds", "grl-manager"), w=8)
timeseries("Snapshot restores / 30s",
           q_scraped_counter_rate("grl.manager.snapshot.restores", "grl-manager"), w=8)
timeseries("Snapshot fallbacks / 30s",
           q_scraped_counter_rate("grl.manager.snapshot.fallbacks", "grl-manager"), w=8)
timeseries("Snapshot evictions / 30s",
           q_scraped_counter_rate("grl.manager.snapshot.evictions", "grl-manager"), w=8)

# 6. GPU (DCGM, scraped) ------------------------------------------------------
row("Network")
timeseries("Node transmit throughput", q_node_exporter_rate("node_network_transmit_bytes_total"),
           w=8, unit="Bps")
timeseries("Node receive throughput", q_node_exporter_rate("node_network_receive_bytes_total"),
           w=8, unit="Bps")
timeseries("Node TX / RX error rate",
           q_node_exporter_rate(("node_network_transmit_errs_total", "node_network_receive_errs_total")),
           w=8, unit="ops")
timeseries("Node TX / RX drop rate",
           q_node_exporter_rate(("node_network_transmit_drop_total", "node_network_receive_drop_total")),
           w=8, unit="ops")
timeseries("TCP retransmission rate", q_node_exporter_rate("node_netstat_Tcp_RetransSegs"),
           w=8, unit="ops")
timeseries("Linux softnet drops / time squeeze",
           q_node_exporter_rate(("node_softnet_dropped_total", "node_softnet_times_squeezed_total")),
           w=8, unit="ops")

# 7. GPU (DCGM, scraped) ------------------------------------------------------
row("GPU")
timeseries("GPU utilization %",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_GPU_UTIL"), w=8, unit="percent")
timeseries("Framebuffer used (MiB)",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_FB_USED"), w=8, unit="decmbytes")
timeseries("Power usage (W)",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_POWER_USAGE"), w=8, unit="watt")
timeseries("GPU temperature (C)",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_GPU_TEMP"), w=8, unit="celsius")
timeseries("SM clock (MHz)",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_SM_CLOCK"), w=8, unit="rotmhz")
timeseries("Framebuffer free (MiB)",
           q_dcgm_gpu_gauge("DCGM_FI_DEV_FB_FREE"), w=8, unit="decmbytes")

# 8. Ray (scraped) ------------------------------------------------------------
row("Ray")
timeseries("Node CPU utilization",
           q_scraped_gauge("ray_node_cpu_utilization", "ray", "pod"), w=8, unit="percent")
timeseries("Node memory used",
           q_scraped_gauge("ray_node_mem_used", "ray", "pod"), w=8, unit="bytes")
timeseries("Object store used memory",
           q_scraped_gauge("ray_object_store_used_memory", "ray", "pod"), w=8, unit="bytes")
timeseries("Pods by Ray group",
           q_scraped_pods_by_attr("ray_node_cpu_utilization", "ray", "ray_group"), w=8)

# 9. Trajectories -------------------------------------------------------------
row("Trajectories")
custom_panel(
    "grl-traces-panel",
    "Recent trajectories (full body)",
    "SELECT TimeUnix AS time, TaskId, GroupId, RolloutIndex, Reward, NumTurns,\n"
    "       DoneReason, PromptTokens, ResponseTokens, PolicyVersionStart,\n"
    "       PolicyVersionCurrent, Body\n"
    "FROM default.grl_trajectories\n"
    "WHERE RunId = '${run_id}'\n"
    "ORDER BY TimeUnix DESC\nLIMIT 500",
    options={"bodyPreviewLength": 60, "pageSize": 25},
    w=24,
    h=24,
    full_width=True,
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
            "definition": "SELECT RunId FROM default.grl_metrics WHERE RunId != '' GROUP BY RunId ORDER BY max(TimeUnix) DESC",
            "hide": 0, "includeAll": False, "label": "Run", "multi": False,
            "name": "run_id", "options": [],
            "query": "SELECT RunId FROM default.grl_metrics WHERE RunId != '' GROUP BY RunId ORDER BY max(TimeUnix) DESC",
            "refresh": 1, "regex": "", "skipUrlSync": False, "sort": 0, "type": "query",
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
    # Grafana does not replace a database dashboard with a provisioned file
    # whose version is older. Bump this whenever the generated dashboard
    # changes so an existing Grafana PVC receives the new layout/query.
    "version": 3,
    "weekStart": "",
}

with open("grl-dashboard.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print(f"wrote grl-dashboard.json with {len(panels)} panels")
