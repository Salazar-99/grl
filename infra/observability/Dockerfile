# Build from infra/observability:
#   docker buildx build --platform linux/arm64 --push -t ghcr.io/salazar-99/grl-grafana:latest .
ARG GRAFANA_VERSION=12.0.0
FROM grafana/grafana:${GRAFANA_VERSION} AS plugins

USER root
RUN grafana cli plugins install grafana-clickhouse-datasource

FROM node:22-bookworm AS panel
WORKDIR /src
COPY grafana/traces-panel/package*.json ./
RUN npm ci
COPY grafana/traces-panel/ ./
RUN npm run build

FROM grafana/grafana:${GRAFANA_VERSION}
USER root
COPY --from=panel /src/dist /var/lib/grafana/plugins/grl-traces-panel
COPY --from=plugins /var/lib/grafana/plugins /var/lib/grafana/plugins
ENV GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=grl-traces-panel
USER grafana
