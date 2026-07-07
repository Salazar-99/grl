resource "helm_release" "resources" {
  name      = var.release_name
  chart     = "${path.module}/chart"
  namespace = var.release_namespace

  values = [
    yamlencode({
      rayCluster = {
        name      = var.ray_cluster_name
        namespace = var.ray_cluster_namespace
        version   = var.ray_version
        images = {
          head     = var.ray_head_image
          rollouts = var.ray_rollouts_image
          training = var.ray_training_image
        }
        workers = {
          rollouts = {
            gpusPerNode = var.ray_rollouts_gpus_per_node
            replicas    = var.ray_rollouts_replicas
          }
          training = {
            gpusPerNode = var.ray_training_gpus_per_node
            replicas    = var.ray_training_replicas
          }
        }
      }
      otelCollector = {
        name      = var.otel_collector_name
        namespace = var.otel_collector_namespace
        upstream = {
          endpoint = var.otel_upstream_endpoint
          username = var.otel_upstream_username
          password = var.otel_upstream_password
        }
      }
      manager = {
        image = var.manager_image
      }
      vmImageCache = {
        bucket = var.vm_images_bucket
        region = var.vm_images_region
      }
      modelCache = {
        tag              = var.model_tag
        revision         = var.model_revision
        huggingfaceToken = var.huggingface_token
      }
    })
  ]
}
