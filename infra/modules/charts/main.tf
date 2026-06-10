resource "helm_release" "opentelemetry_operator" {
  name             = "opentelemetry-operator"
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-operator"
  version          = var.opentelemetry_operator_chart_version
  namespace        = var.opentelemetry_operator_namespace
  create_namespace = true
}

resource "helm_release" "nvidia_gpu_operator" {
  name             = "gpu-operator"
  repository       = "https://helm.ngc.nvidia.com/nvidia"
  chart            = "gpu-operator"
  version          = var.nvidia_gpu_operator_chart_version
  namespace        = var.nvidia_gpu_operator_namespace
  create_namespace = true
}

resource "helm_release" "kuberay_operator" {
  name             = "kuberay-operator"
  repository       = "https://ray-project.github.io/kuberay-helm/"
  chart            = "kuberay-operator"
  version          = var.kuberay_operator_chart_version
  namespace        = var.kuberay_operator_namespace
  create_namespace = true
}
