output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.cluster.cluster_name}"
}
