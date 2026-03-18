# ── MILESTONE 1 REFACTORING NOTE ─────────────────────────────────────────
#
# This file previously contained the EC2 (kubeadm) resources for the
# OpenClaw AlgoTrader system. All resources have been removed and replaced
# by dedicated files as part of the YouTube AI Factory transformation:
#
#   providers.tf  — terraform{} block, AWS / Helm / kubectl providers
#   vpc.tf        — VPC data sources, EKS subnets, NAT GW, S3 VPC endpoint
#   eks.tf        — EKS managed cluster + light-agents node group
#   karpenter.tf  — Karpenter controller Helm release + NodePool CRDs
#   s3.tf         — Four media pipeline S3 buckets + lifecycle rules
#   iam.tf        — Per-agent IRSA roles (least-privilege S3 + Secrets)
#   secrets.tf    — Secrets Manager secret for all third-party API keys
#   billing.tf    — CloudWatch billing alarms at $20 and $50 thresholds
#   variables.tf  — All input variables (no financial/trading vars remain)
#   outputs.tf    — Cluster endpoint, bucket names, IRSA ARNs
#
# This file is intentionally empty of resources.
# ─────────────────────────────────────────────────────────────────────────
