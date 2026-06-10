# --- IAM: Cluster ---

resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# --- IAM: Nodes (shared across all node groups) ---

resource "aws_iam_role" "nodes" {
  name = "${var.cluster_name}-nodes"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "nodes_worker" {
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "nodes_cni" {
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "nodes_ecr" {
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# --- EKS Cluster ---

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = concat(var.subnet_ids, var.node_subnet_ids)
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  depends_on = [aws_iam_role_policy_attachment.cluster_policy]
}

# --- Node Groups ---

resource "aws_eks_node_group" "ray" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "ray"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.node_subnet_ids
  instance_types  = var.ray.instance_types
  ami_type        = var.ray.ami_type
  disk_size       = var.ray.disk_size

  scaling_config {
    desired_size = var.ray.desired_size
    min_size     = var.ray.min_size
    max_size     = var.ray.max_size
  }

  labels = { role = "ray" }

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_ecr,
  ]
}

resource "aws_eks_node_group" "rollouts" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "rollouts"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.node_subnet_ids
  instance_types  = var.rollouts.instance_types
  ami_type        = var.rollouts.ami_type
  disk_size       = var.rollouts.disk_size

  scaling_config {
    desired_size = var.rollouts.desired_size
    min_size     = var.rollouts.min_size
    max_size     = var.rollouts.max_size
  }

  labels = { role = "rollouts" }

  # Prevents non-GPU workloads from landing on expensive GPU nodes
  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_ecr,
  ]
}

resource "aws_eks_node_group" "training" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "training"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.node_subnet_ids
  instance_types  = var.training.instance_types
  ami_type        = var.training.ami_type
  disk_size       = var.training.disk_size

  scaling_config {
    desired_size = var.training.desired_size
    min_size     = var.training.min_size
    max_size     = var.training.max_size
  }

  labels = { role = "training" }

  # Prevents non-GPU workloads from landing on expensive GPU nodes
  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_ecr,
  ]
}

resource "aws_eks_node_group" "environment" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "environment"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = var.node_subnet_ids
  instance_types  = var.environment.instance_types
  ami_type        = var.environment.ami_type
  disk_size       = var.environment.disk_size

  scaling_config {
    desired_size = var.environment.desired_size
    min_size     = var.environment.min_size
    max_size     = var.environment.max_size
  }

  # kvm=true marks these bare-metal nodes as able to expose /dev/kvm, so
  # Firecracker pods can nodeSelector onto them.
  labels = {
    role = "environment"
    kvm  = "true"
  }

  depends_on = [
    aws_iam_role_policy_attachment.nodes_worker,
    aws_iam_role_policy_attachment.nodes_cni,
    aws_iam_role_policy_attachment.nodes_ecr,
  ]
}

