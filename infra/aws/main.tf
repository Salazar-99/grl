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

  node_groups = var.node_groups

  vm_images_bucket = var.vm_images_bucket
}

module "charts" {
  count  = var.deploy_workloads ? 1 : 0
  source = "../modules/charts"

  opentelemetry_operator_chart_version = var.opentelemetry_operator_chart_version
  nvidia_gpu_operator_chart_version    = var.nvidia_gpu_operator_chart_version
  kuberay_operator_chart_version       = var.kuberay_operator_chart_version

  depends_on = [module.cluster]
}

module "resources" {
  count  = var.deploy_workloads ? 1 : 0
  source = "../modules/resources"

  release_name                       = var.release_name
  release_namespace                  = var.release_namespace
  ray_cluster_name                   = var.ray_cluster_name
  ray_cluster_namespace              = var.ray_cluster_namespace
  ray_head_image                     = var.ray_head_image
  ray_rollouts_image                 = var.ray_rollouts_image
  ray_training_image                 = var.ray_training_image
  ray_version                        = var.ray_version
  manager_image                      = var.manager_image
  manager_snapshots_enabled          = var.manager_snapshots_enabled
  manager_snapshot_cache_max_entries = var.manager_snapshot_cache_max_entries

  ray_rollouts_gpus_per_node = var.ray_rollouts_gpus_per_node
  ray_training_gpus_per_node = var.ray_training_gpus_per_node
  ray_rollouts_replicas      = var.ray_rollouts_replicas
  ray_training_replicas      = var.ray_training_replicas

  vm_images_bucket     = var.vm_images_bucket
  vm_images_region     = var.vm_images_region != "" ? var.vm_images_region : var.region
  vm_images_scratch_gb = var.vm_images_scratch_gb
  vm_bootstrap_key     = var.vm_bootstrap_key

  model_tag         = var.model_tag
  model_revision    = var.model_revision
  huggingface_token = var.huggingface_token

  otel_collector_name      = var.otel_collector_name
  otel_collector_namespace = var.otel_collector_namespace
  otel_upstream_endpoint   = var.otel_upstream_endpoint
  otel_upstream_username   = var.otel_upstream_username
  otel_upstream_password   = var.otel_upstream_password

  depends_on = [module.charts]
}
