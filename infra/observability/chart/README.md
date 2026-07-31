# GRL observability chart

This chart deploys the standalone persistent ClickHouse backend, HTTP-only
remote OTel collector, Grafana, GRL schema/materialized views, dashboard, and
the `grl-traces-panel` plugin. It is independent of `grl-resources`, Gaia,
and the ClickHouse or OpenTelemetry operators.

At minimum set `global.domain`; the chart creates `otel.<domain>` and
`grafana.<domain>` Ingresses. Set non-default passwords for `clickhouse`,
`otelCollector`, and `grafana`, and configure the existing `grl-resources`
chart's `otelCollector.upstream` to use the collector hostname and the
collector credentials. The in-cluster collector remains the OTLP gRPC entry
point for GRL services and forwards OTLP/HTTP to this chart.

The default Grafana image is `ghcr.io/grl/grl-grafana:latest`. Build and push it
from the repository root with:

```sh
docker build -f infra/observability/Dockerfile \
  -t ghcr.io/grl/grl-grafana:latest .
docker push ghcr.io/grl/grl-grafana:latest
```

TLS, cert-manager annotations, ingress classes, storage classes, scheduling,
resources, and retention are configurable in `values.yaml`. Back up the
ClickHouse PVC or export its GRL tables; storage is durable but no backup job
is included.
