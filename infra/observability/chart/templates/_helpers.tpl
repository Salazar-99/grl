{{- define "grl-observability.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "grl-observability.fullname" -}}
{{- if .Values.fullnameOverride }}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}{{- else }}{{ include "grl-observability.name" . }}{{- end -}}
{{- end -}}
{{- define "grl-observability.labels" -}}
app.kubernetes.io/name: {{ include "grl-observability.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}
{{- define "grl-observability.selector" -}}
app.kubernetes.io/name: {{ include "grl-observability.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "grl-observability.domain" -}}
{{- required "global.domain is required (for example example.com)" .Values.global.domain -}}
{{- end -}}
{{- define "grl-observability.clickhouse" -}}{{ include "grl-observability.fullname" . }}-clickhouse{{- end -}}
{{- define "grl-observability.collector" -}}{{ include "grl-observability.fullname" . }}-otel{{- end -}}
{{- define "grl-observability.grafana" -}}{{ include "grl-observability.fullname" . }}-grafana{{- end -}}
