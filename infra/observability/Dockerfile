# Build from the repository root:
#   docker buildx build --platform linux/arm64 --push \
#     -f infra/observability/Dockerfile -t ghcr.io/salazar-99/grl-grafana:latest .
ARG GRAFANA_VERSION=13.0.2
FROM grafana/grafana:${GRAFANA_VERSION} AS plugins

USER root
RUN grafana cli plugins install grafana-clickhouse-datasource

FROM node:22-bookworm AS panel
WORKDIR /src
COPY infra/observability/grafana/traces-panel/package*.json ./
RUN npm ci
COPY infra/observability/grafana/traces-panel/ ./
RUN npm run build

FROM grafana/grafana:${GRAFANA_VERSION} AS grafana
USER root
COPY --from=panel /src/dist /var/lib/grafana/plugins/grl-traces-panel
COPY --from=plugins /var/lib/grafana/plugins /var/lib/grafana/plugins
ENV GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=grl-traces-panel
USER grafana
