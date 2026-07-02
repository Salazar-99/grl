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

variable "node_groups" {
  description = "Instance types, AMI, disk, and fixed node count for each EKS node group."
  type = object({
    ray = object({
      instance_types = list(string)
      ami_type       = string
      disk_size      = number
      node_count     = number
    })
    rollouts = object({
      instance_types = list(string)
      ami_type       = string
      disk_size      = number
      node_count     = number
    })
    training = object({
      instance_types = list(string)
      ami_type       = string
      disk_size      = number
      node_count     = number
    })
    environments = object({
      instance_types = list(string)
      ami_type       = string
      disk_size      = number
      node_count     = number
    })
  })
  default = {
    ray = {
      instance_types = ["m5.4xlarge"]
      ami_type       = "AL2023_x86_64_STANDARD"
      disk_size      = 100
      node_count     = 2
    }
    rollouts = {
      instance_types = ["g4dn.xlarge"]
      ami_type       = "AL2023_x86_64_NVIDIA"
      disk_size      = 200
      node_count     = 1
    }
    training = {
      instance_types = ["g4dn.xlarge"]
      ami_type       = "AL2023_x86_64_NVIDIA"
      disk_size      = 200
      node_count     = 1
    }
    environments = {
      instance_types = ["c5.metal"]
      ami_type       = "AL2023_x86_64_STANDARD"
      disk_size      = 200
      node_count     = 1
    }
  }

  validation {
    condition     = alltrue([for t in var.node_groups.environments.instance_types : endswith(t, ".metal")])
    error_message = "node_groups.environments.instance_types must be bare-metal (.metal) instances so /dev/kvm is available for Firecracker."
  }
}

variable "vm_images_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding Firecracker VM artifacts (kernel/, bases/, tasks/) that must match the VMS_S3_BUCKET used by environments/swebench-lite/vms uploads. Empty disables the vm-image-cache DaemonSet and its IAM policy."
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
