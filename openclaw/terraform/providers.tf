terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = "~> 1.14"
    }
  }
}

# ── Primary region provider ───────────────────────────────────────────────
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "yt-factory"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ── us-east-1 alias — required for CloudWatch Billing alarms (billing
#    metrics are only published in us-east-1 regardless of workload region)
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "yt-factory"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ── Helm provider — wired to EKS cluster via AWS CLI token exec ───────────
# NOTE: On a brand-new cluster the first `terraform apply` must be split into
# two phases to resolve the chicken-and-egg dependency:
#   Phase 1: terraform apply -target=module.eks
#   Phase 2: terraform apply          (installs Karpenter via Helm)
provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
      command     = "aws"
    }
  }
}

# ── kubectl provider — used to apply Karpenter CRDs (NodePool, EC2NodeClass)
provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  load_config_file       = false

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    command     = "aws"
  }
}
