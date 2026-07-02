variable "kubeconfig_path" {
  type        = string
  description = "Path to a kubeconfig file. The default/current context in that file is used."
}

variable "opentelemetry_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the OpenTelemetry Operator."
}

variable "nvidia_gpu_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the NVIDIA GPU Operator."
}

variable "kuberay_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the KubeRay Operator."
}

variable "release_name" {
  type        = string
  default     = "grl-resources"
  description = "Helm release name for the GRL resources chart."
}

variable "release_namespace" {
  type        = string
  default     = "default"
  description = "Namespace for the GRL resources Helm release metadata."
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
  description = "Ray container image used by rollouts worker pods."
}

variable "ray_training_image" {
  type        = string
  default     = "grl-training:training"
  description = "Ray container image used by training worker pods."
}

variable "ray_version" {
  type        = string
  default     = "2.55.1"
  description = "Ray version reported in the RayCluster spec."
}

variable "ray_rollouts_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per rollouts worker node."
}

variable "ray_training_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per training worker node."
}

variable "manager_image" {
  type        = string
  default     = "grl-manager:latest"
  description = "Environment manager image run as a DaemonSet."
}

variable "vm_images_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding Firecracker VM artifacts."
}

variable "vm_images_region" {
  type        = string
  default     = "us-west-2"
  description = "AWS region of the VM images bucket."
}

variable "model_tag" {
  type        = string
  default     = ""
  description = "Hugging Face model repo id to cache on GPU nodes."
}

variable "model_revision" {
  type        = string
  default     = ""
  description = "Optional Hugging Face revision for model_tag."
}

variable "huggingface_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Hugging Face Hub token for model-cache downloads."
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
  description = "OTLP gRPC endpoint of the external collector."
}

variable "otel_upstream_username" {
  type        = string
  default     = ""
  description = "Basic-auth username for the external OTLP collector."
}

variable "otel_upstream_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Basic-auth password for the external OTLP collector."
}
