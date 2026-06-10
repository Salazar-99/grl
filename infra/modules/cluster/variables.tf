variable "cluster_name" {
  type    = string
  default = "grl"
}

variable "cluster_version" {
  type    = string
  default = "1.32"
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnets for the EKS control plane."
}

variable "node_subnet_ids" {
  type        = list(string)
  description = "Subnets for node groups (typically private)."
}

variable "ray" {
  description = "Node group running the Ray head and CPU worker pods."
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

variable "rollouts" {
  description = "Node group running GPU vLLM rollout (inference) pods."
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
    # AL2023 NVIDIA AMI. AL2-based AMI types are deprecated and unavailable on
    # EKS 1.33+; AL2023_x86_64_NVIDIA ships the NVIDIA driver and is current.
    ami_type     = "AL2023_x86_64_NVIDIA"
    disk_size    = 200
    desired_size = 1
    min_size     = 0
    max_size     = 8
  }
}

variable "training" {
  description = "Node group running GPU training pods."
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

variable "environment" {
  description = <<-EOT
    Node group running environment workers that launch Firecracker microVMs from
    within their pods. Firecracker needs /dev/kvm, which on EC2 is only exposed
    on bare-metal (.metal) instance types, so instance_types must be bare metal.
    A single metal node hosts many microVMs, so this group scales on fewer,
    larger nodes than the others.
  EOT
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

  validation {
    condition     = alltrue([for t in var.environment.instance_types : endswith(t, ".metal")])
    error_message = "environment.instance_types must be bare-metal (.metal) instances so /dev/kvm is available for Firecracker."
  }
}
