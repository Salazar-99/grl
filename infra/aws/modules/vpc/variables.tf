variable "name" {
  type    = string
  default = "grl"
}

variable "cluster_name" {
  type = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "az_count" {
  type        = number
  default     = 2
  description = "Number of availability zones for public and private subnets. EKS requires at least 2."

  validation {
    condition     = var.az_count >= 2
    error_message = "az_count must be at least 2 because EKS requires subnets in at least two availability zones."
  }
}
