# Maps EC2 GPU instance types to their physical GPU count, so the RayCluster
# can advertise the right number of GPUs per node without hand-editing the chart.
# Add new instance types here as they are adopted; unknown types fall back to 1.
locals {
  gpus_per_instance = {
    # g4dn (NVIDIA T4)
    "g4dn.xlarge"   = 1
    "g4dn.2xlarge"  = 1
    "g4dn.4xlarge"  = 1
    "g4dn.8xlarge"  = 1
    "g4dn.16xlarge" = 1
    "g4dn.12xlarge" = 4
    "g4dn.metal"    = 8

    # g5 (NVIDIA A10G)
    "g5.xlarge"   = 1
    "g5.2xlarge"  = 1
    "g5.4xlarge"  = 1
    "g5.8xlarge"  = 1
    "g5.16xlarge" = 1
    "g5.12xlarge" = 4
    "g5.24xlarge" = 4
    "g5.48xlarge" = 8

    # p3 (NVIDIA V100)
    "p3.2xlarge"  = 1
    "p3.8xlarge"  = 4
    "p3.16xlarge" = 8

    # p4d / p5 (NVIDIA A100 / H100)
    "p4d.24xlarge" = 8
    "p5.48xlarge"  = 8
  }

  # GPU node groups are homogeneous, so the first instance type sets the count.
  rollouts_gpu_count = lookup(local.gpus_per_instance, var.rollouts_nodes.instance_types[0], 1)
  training_gpu_count = lookup(local.gpus_per_instance, var.training_nodes.instance_types[0], 1)
}
