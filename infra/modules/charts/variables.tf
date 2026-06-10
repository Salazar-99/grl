variable "cluster_name" {
  type = string
}

variable "cluster_endpoint" {
  type = string
}

variable "cluster_ca_certificate" {
  type = string
}

variable "opentelemetry_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the OpenTelemetry Operator. When null, Helm installs the latest available chart."
}

variable "opentelemetry_operator_namespace" {
  type        = string
  default     = "opentelemetry-operator-system"
  description = "Kubernetes namespace for the OpenTelemetry Operator Helm release."
}

variable "nvidia_gpu_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the NVIDIA GPU Operator. When null, Helm installs the latest available chart."
}

variable "nvidia_gpu_operator_namespace" {
  type        = string
  default     = "gpu-operator"
  description = "Kubernetes namespace for the NVIDIA GPU Operator Helm release."
}

variable "kuberay_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the KubeRay Operator. When null, Helm installs the latest available chart."
}

variable "kuberay_operator_namespace" {
  type        = string
  default     = "default"
  description = "Kubernetes namespace for the KubeRay Operator Helm release."
}
