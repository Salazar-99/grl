variable "region" {
  type    = string
  default = "us-west-2"
}

variable "cluster_name" {
  type    = string
  default = "grl"
}

variable "cluster_version" {
  type        = string
  default     = "1.32"
  description = "Kubernetes version for the EKS control plane and node groups."
}

variable "ray_nodes" {
  description = "Instance types, AMI, disk, and scaling config for the Ray node group."
  type = object({
    instance_types = list(string)
    ami_type       = string
    disk_size      = number
    desired_size   = number
    min_size       = number
    max_size       = number
  })
  default = {
    instance_types = ["m5.4xlarge"]
    ami_type       = "AL2023_x86_64_STANDARD"
    disk_size      = 100
    desired_size   = 2
    min_size       = 1
    max_size       = 10
  }
}

variable "rollouts_nodes" {
  description = "Instance types, AMI, disk, and scaling config for the rollouts (GPU vLLM inference) node group."
  type = object({
    instance_types = list(string)
    ami_type       = string
    disk_size      = number
    desired_size   = number
    min_size       = number
    max_size       = number
  })
  default = {
    instance_types = ["g4dn.xlarge"]
    ami_type       = "AL2023_x86_64_NVIDIA"
    disk_size      = 200
    desired_size   = 1
    min_size       = 0
    max_size       = 8
  }
}

variable "training_nodes" {
  description = "Instance types, AMI, disk, and scaling config for the training (GPU) node group. TrainingWorker places the policy on cuda:0 and the reference model on cuda:1, so this group needs an instance type with at least 2 GPUs."
  type = object({
    instance_types = list(string)
    ami_type       = string
    disk_size      = number
    desired_size   = number
    min_size       = number
    max_size       = number
  })
  default = {
    instance_types = ["g4dn.xlarge"]
    ami_type       = "AL2023_x86_64_NVIDIA"
    disk_size      = 200
    desired_size   = 1
    min_size       = 0
    max_size       = 8
  }
}

variable "environment_nodes" {
  description = "Instance types, AMI, disk, and scaling config for the environment node group. Instance types must be bare metal (.metal) for Firecracker KVM access."
  type = object({
    instance_types = list(string)
    ami_type       = string
    disk_size      = number
    desired_size   = number
    min_size       = number
    max_size       = number
  })
  default = {
    instance_types = ["c5.metal"]
    ami_type       = "AL2023_x86_64_STANDARD"
    disk_size      = 200
    desired_size   = 1
    min_size       = 1
    max_size       = 10
  }
}

variable "opentelemetry_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the OpenTelemetry Operator. When null, Helm installs the latest available chart."
}

variable "nvidia_gpu_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the NVIDIA GPU Operator. When null, Helm installs the latest available chart."
}

variable "kuberay_operator_chart_version" {
  type        = string
  default     = null
  description = "Helm chart version for the KubeRay Operator. When null, Helm installs the latest available chart."
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
