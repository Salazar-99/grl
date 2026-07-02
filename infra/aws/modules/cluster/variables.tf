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

variable "vm_images_bucket" {
  type        = string
  default     = ""
  description = "S3 bucket holding Firecracker VM artifacts. When set, the node role gets read access so the vm-image-cache DaemonSet can sync it. Empty skips the policy."
}

variable "node_groups" {
  description = <<-EOT
    Role-keyed EKS node group settings. node_count is fixed; no autoscaling is
    configured. The environments group runs workers that launch Firecracker
    microVMs and must use bare-metal (.metal) instances for /dev/kvm access.
  EOT
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
