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

variable "deploy_workloads" {
  type        = bool
  default     = true
  description = "When false, provision only the VPC + EKS cluster (skip the operator charts and the GRL resources chart). Set false by the launcher CLUSTER layer, true by RESOURCES/FULL."
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

variable "vm_images_region" {
  type        = string
  default     = ""
  description = "AWS region of the VM images bucket. Empty falls back to var.region."
}

variable "vm_images_scratch_gb" {
  type        = number
  default     = 2
  description = "Size (GiB) of the per-VM scratch template staged by vm-image-cache."
}

variable "vm_bootstrap_key" {
  type        = string
  default     = ""
  description = "Immutable S3 key for the active Firecracker initrd."
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

variable "manager_snapshots_enabled" {
  type        = bool
  default     = false
  description = "Enable node-local Firecracker golden snapshots."
}

variable "manager_snapshot_cache_max_entries" {
  type        = string
  default     = "64"
  description = "Maximum node-local golden snapshot entries."
}

variable "manager_use_jailer" {
  type        = bool
  default     = false
  description = "Run Firecracker through its jailer."
}

variable "manager_jailer_root" {
  type        = string
  default     = "/srv/jailer"
  description = "Base directory for per-VM Firecracker jail roots."
}

variable "ray_version" {
  type        = string
  default     = "2.55.1"
  description = "Ray version reported in the RayCluster spec."
}

variable "ray_rollouts_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per rollouts worker node (Ray num-gpus, the rollouts custom resource, and the pod nvidia.com/gpu request). Resolved by the launcher from compute."
}

variable "ray_training_gpus_per_node" {
  type        = number
  default     = 1
  description = "GPUs advertised per training worker node (Ray num-gpus, the training custom resource, and the pod nvidia.com/gpu request). Resolved by the launcher from compute."
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
  default     = ""
  description = "Basic-auth username for the external OTLP collector. Only used when deploy_workloads is true."
}

variable "otel_upstream_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Basic-auth password for the external OTLP collector. Only used when deploy_workloads is true."
}
