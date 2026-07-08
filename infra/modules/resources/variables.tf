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

variable "manager_image" {
  type        = string
  default     = "grl-manager:latest"
  description = "Environment manager image run as a DaemonSet on environment nodes (environments/manager/Dockerfile)."
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

variable "ray_rollouts_replicas" {
  type        = number
  default     = 1
  description = "KubeRay rollouts worker pod count. Matches compute.rollouts.nodes."
}

variable "ray_training_replicas" {
  type        = number
  default     = 1
  description = "KubeRay training worker pod count. Matches compute.training.nodes."
}

variable "vm_images_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding Firecracker VM artifacts (kernel, base/task images) — the vms tooling's VMS_S3_BUCKET. Empty disables the vm-image-cache DaemonSet."
}

variable "vm_images_region" {
  type        = string
  description = "AWS region of the VM images bucket."
}

variable "model_tag" {
  type        = string
  default     = ""
  description = "Hugging Face model repo id to cache on GPU nodes (e.g. Qwen/Qwen2.5-7B). Weights are stored at /models/<repo-name>. Empty disables the model-cache DaemonSet."
}

variable "model_revision" {
  type        = string
  default     = ""
  description = "Optional Hugging Face revision (branch, tag, or commit) for model_tag. Empty uses the repo default."
}

variable "huggingface_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Hugging Face Hub token for authenticated downloads in the model-cache DaemonSet (higher rate limits, gated models, hf_transfer)."
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
  default     = "https://otel.gerardosalazar.com"
  description = "OTLP/HTTP endpoint (URL) of the external collector that owns the ClickHouse export. Always TLS; the otlphttp exporter appends /v1/{traces,metrics,logs}."
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
