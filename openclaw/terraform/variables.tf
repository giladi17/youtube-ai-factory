# ── General ───────────────────────────────────────────────────────────────
variable "region" {
  description = "Primary AWS region for all workload resources"
  type        = string
  default     = "eu-north-1"
}

variable "environment" {
  description = "Deployment environment label (prod | staging)"
  type        = string
  default     = "prod"
}

# ── Networking ────────────────────────────────────────────────────────────
variable "vpc_id" {
  description = "ID of the existing VPC to deploy EKS and subnets into"
  type        = string
}

variable "private_subnet_cidrs" {
  description = "Two CIDR blocks for private EKS subnets (must be in different AZs, must not overlap existing subnets)"
  type        = list(string)
  default     = ["172.31.64.0/20", "172.31.80.0/20"]

  validation {
    condition     = length(var.private_subnet_cidrs) == 2
    error_message = "Exactly 2 private subnet CIDRs are required for EKS multi-AZ."
  }
}

variable "public_subnet_cidrs" {
  description = "Two CIDR blocks for public subnets (NAT Gateway + future ALB)"
  type        = list(string)
  default     = ["172.31.96.0/20", "172.31.112.0/20"]

  validation {
    condition     = length(var.public_subnet_cidrs) == 2
    error_message = "Exactly 2 public subnet CIDRs are required."
  }
}

# ── EKS Cluster ───────────────────────────────────────────────────────────
variable "cluster_name" {
  description = "Name of the EKS cluster (also used as Karpenter discovery tag value)"
  type        = string
  default     = "yt-factory"
}

variable "cluster_version" {
  description = "Kubernetes version for the EKS control plane"
  type        = string
  default     = "1.30"
}

# ── Karpenter ─────────────────────────────────────────────────────────────
variable "karpenter_version" {
  description = "Karpenter Helm chart version (must be >= 1.0.0 for v1 API support)"
  type        = string
  default     = "1.0.0"
}

# ── API Keys (sensitive — never commit to git) ────────────────────────────
variable "openai_api_key" {
  description = "OpenAI API key — used by Scriptwriter (GPT-4o topic selection + script) and SEO Publisher (metadata generation)"
  type        = string
  sensitive   = true
}

variable "heygen_api_key" {
  description = "HeyGen API key — used by Avatar Director to trigger lip-sync video renders"
  type        = string
  sensitive   = true
}

variable "heygen_renewal_date" {
  description = "HeyGen subscription renewal date (YYYY-MM-DD). Stored in Secrets Manager as a reminder. No auto-renewal."
  type        = string
}

variable "youtube_client_id" {
  description = "YouTube Data API v3 OAuth2 Client ID (from Google Cloud Console)"
  type        = string
  sensitive   = true
}

variable "youtube_client_secret" {
  description = "YouTube Data API v3 OAuth2 Client Secret"
  type        = string
  sensitive   = true
}

variable "youtube_refresh_token" {
  description = "YouTube Data API v3 long-lived OAuth2 refresh token (obtained once via consent flow)"
  type        = string
  sensitive   = true
}

# ── Container Registry ────────────────────────────────────────────────────
variable "dockerhub_username" {
  description = "DockerHub username for pulling agent images"
  type        = string
}

variable "dockerhub_token" {
  description = "DockerHub access token (read-only scope is sufficient)"
  type        = string
  sensitive   = true
}

# ── FinOps ────────────────────────────────────────────────────────────────
variable "billing_alert_email" {
  description = "Email address for CloudWatch billing alarm notifications ($20 and $50 thresholds)"
  type        = string
}
