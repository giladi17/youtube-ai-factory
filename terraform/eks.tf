# ── EKS Cluster — managed control plane replaces kubeadm EC2 master ───────
# Uses the community terraform-aws-modules/eks module (v20) which provisions:
#   - EKS control plane + IAM roles
#   - OIDC provider (required for IRSA — per-agent IAM roles)
#   - Managed node group for always-on light agents
#   - Core add-ons (coredns, kube-proxy, vpc-cni, ebs-csi)
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  # Public endpoint so CI/CD (GitHub Actions) and local kubectl can connect.
  # Lock down cluster_endpoint_public_access_cidrs in production if needed.
  cluster_endpoint_public_access = true

  vpc_id     = data.aws_vpc.main.id
  subnet_ids = aws_subnet.private[*].id

  # ── EKS Managed Add-ons ───────────────────────────────────────────────
  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent = true
    }
    # EBS CSI driver — required if any agent needs persistent volumes
    aws-ebs-csi-driver = {
      most_recent              = true
      service_account_role_arn = aws_iam_role.ebs_csi.arn
    }
  }

  # ── Always-on Managed Node Group for light agents ────────────────────
  # Scriptwriter, Avatar Director, SEO Publisher, and Orchestrator all run
  # here. Spot t3.medium keeps baseline cost minimal (~$0.015/hr per node).
  eks_managed_node_groups = {
    light_agents = {
      name = "light-agents"

      instance_types = ["t3.medium", "t3a.medium"] # fallback for spot availability
      capacity_type  = "SPOT"

      min_size     = 1
      max_size     = 5
      desired_size = 1

      labels = {
        "node.kubernetes.io/pool" = "light-agents"
      }

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            delete_on_termination = true
          }
        }
      }

      tags = {
        "karpenter.sh/discovery" = var.cluster_name
      }
    }
  }

  # Grant the Terraform caller admin access on the cluster so CI/CD can apply
  # K8s manifests without a separate aws-auth ConfigMap patch
  enable_cluster_creator_admin_permissions = true

  tags = {
    # Karpenter uses this tag to discover which EKS cluster to join nodes to
    "karpenter.sh/discovery" = var.cluster_name
  }
}

# ── EBS CSI IAM Role (IRSA) ───────────────────────────────────────────────
resource "aws_iam_role" "ebs_csi" {
  name = "${var.cluster_name}-ebs-csi-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
