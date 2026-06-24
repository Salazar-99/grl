module "vpc" {
  source       = "./modules/vpc"
  name         = var.cluster_name
  cluster_name = var.cluster_name
}

module "cluster" {
  source          = "./modules/cluster"
  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version
  subnet_ids      = module.vpc.public_subnet_ids
  node_subnet_ids = module.vpc.private_subnet_ids

  ray         = var.ray_nodes
  rollouts    = var.rollouts_nodes
  training    = var.training_nodes
  environment = var.environment_nodes

  vm_images_bucket = var.vm_images_bucket
}

module "charts" {
  source = "./modules/charts"

  cluster_name           = module.cluster.cluster_name
  cluster_endpoint       = module.cluster.cluster_endpoint
  cluster_ca_certificate = module.cluster.cluster_ca_certificate

  opentelemetry_operator_chart_version = var.opentelemetry_operator_chart_version
  nvidia_gpu_operator_chart_version    = var.nvidia_gpu_operator_chart_version
  kuberay_operator_chart_version       = var.kuberay_operator_chart_version
}

module "resources" {
  source = "./modules/resources"

  ray_cluster_name      = var.ray_cluster_name
  ray_cluster_namespace = var.ray_cluster_namespace
  ray_head_image        = var.ray_head_image
  ray_rollouts_image    = var.ray_rollouts_image
  ray_training_image    = var.ray_training_image
  ray_version           = var.ray_version

  manager_image = var.manager_image

  # Each GPU worker group has its own node group, so it advertises the per-node
  # GPU count derived from that group's chosen instance type.
  ray_rollouts_gpus_per_node = local.rollouts_gpu_count
  ray_training_gpus_per_node = local.training_gpu_count

  vm_images_bucket = var.vm_images_bucket
  vm_images_region = var.region

  otel_collector_name      = var.otel_collector_name
  otel_collector_namespace = var.otel_collector_namespace
  otel_upstream_endpoint   = var.otel_upstream_endpoint
  otel_upstream_username   = var.otel_upstream_username
  otel_upstream_password   = var.otel_upstream_password

  depends_on = [module.charts]
}
