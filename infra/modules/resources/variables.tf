variable "release_name" {
  type        = string
  default     = "grl-resources"
  description = "Helm release name for the custom resources chart."
}

variable "release_namespace" {
  type        = string
  default     = "default"
  description = "Namespace for the custom resources Helm release metadata."
}

variable "ray_cluster_name" {
  type        = string
  default     = "grl-ray"
  description = "Name for the RayCluster custom resource."
}

variable "ray_cluster_namespace" {
  type        = string
  default     = "default"
  description = "Namespace for the RayCluster custom resource."
}

variable "ray_head_image" {
  type        = string
  default     = "grl-training:head"
  description = "Ray container image used by the RayCluster head pod."
}

variable "ray_rollouts_image" {
  type        = string
  default     = "grl-training:rollouts"
  description = "Ray container image used by rollouts (GPU vLLM) worker pods."
}

variable "ray_training_image" {
  type        = string
  default     = "grl-training:training"
  description = "Ray container image used by training (GPU) worker pods."
}

variable "ray_environment_image" {
  type        = string
  default     = "grl-training:environment"
  description = "Ray container image used by environment worker pods."
}

variable "ray_version" {
  type        = string
  default     = "2.55.1"
  description = "Ray version reported in the RayCluster spec."
}

variable "ray_rollouts_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per rollouts worker node (Ray num-gpus, the rollouts custom resource, and the pod nvidia.com/gpu request)."
}

variable "ray_training_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per training worker node (Ray num-gpus, the training custom resource, and the pod nvidia.com/gpu request)."
}

variable "otel_collector_name" {
  type        = string
  default     = "grl-collector"
  description = "Name for the OpenTelemetryCollector custom resource."
}

variable "otel_collector_namespace" {
  type        = string
  default     = "default"
  description = "Namespace for the OpenTelemetryCollector custom resource."
}

variable "otel_upstream_endpoint" {
  type        = string
  default     = "otel.gerardosalazar.com:4317"
  description = "OTLP gRPC endpoint of the external collector that owns the ClickHouse export. Always TLS."
}

variable "otel_upstream_username" {
  type        = string
  description = "Basic-auth username for the external OTLP collector."
}

variable "otel_upstream_password" {
  type        = string
  sensitive   = true
  description = "Basic-auth password for the external OTLP collector."
}
