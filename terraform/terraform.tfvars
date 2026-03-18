# ── General ───────────────────────────────────────────────────────────────
region      = "eu-north-1"
environment = "prod"

# ── Networking ────────────────────────────────────────────────────────────
# Existing VPC (default VPC in eu-north-1, CIDR 172.31.0.0/16)
vpc_id = "vpc-036a4c9e20ce02383"

# New EKS subnets — private (nodes) and public (NAT GW).
# These CIDRs are chosen to sit above the default-VPC /20 blocks
# (172.31.0/20, 172.31.16/20, 172.31.32/20, 172.31.48/20).
# If any overlap is detected on `terraform plan`, increment the third octet.
private_subnet_cidrs = ["172.31.64.0/20", "172.31.80.0/20"]
public_subnet_cidrs  = ["172.31.96.0/20", "172.31.112.0/20"]

# ── EKS ───────────────────────────────────────────────────────────────────
cluster_name    = "yt-factory"
cluster_version = "1.30"

# ── Karpenter ─────────────────────────────────────────────────────────────
karpenter_version = "1.0.0"

# ── Container Registry ────────────────────────────────────────────────────
dockerhub_username = "giladi17"

# ── FinOps ────────────────────────────────────────────────────────────────
billing_alert_email = "REPLACE_WITH_YOUR_EMAIL@example.com"

# HeyGen subscription renewal — update when you sign up
heygen_renewal_date = "YYYY-MM-DD"

# ─────────────────────────────────────────────────────────────────────────
# SENSITIVE VARIABLES — do NOT add to this file.
# Pass them via environment variables or a gitignored file:
#
#   Option A — CLI flags:
#     terraform apply \
#       -var="openai_api_key=sk-..." \
#       -var="heygen_api_key=..." \
#       -var="youtube_client_id=..." \
#       -var="youtube_client_secret=..." \
#       -var="youtube_refresh_token=..." \
#       -var="dockerhub_token=..."
#
#   Option B — secrets.auto.tfvars (add to .gitignore):
#     openai_api_key        = "sk-..."
#     heygen_api_key        = "..."
#     youtube_client_id     = "..."
#     youtube_client_secret = "..."
#     youtube_refresh_token = "..."
#     dockerhub_token       = "..."
#
#   Option C — TF_VAR_ environment variables:
#     export TF_VAR_openai_api_key="sk-..."
# ─────────────────────────────────────────────────────────────────────────
