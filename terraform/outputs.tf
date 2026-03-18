# ── EKS ───────────────────────────────────────────────────────────────────
output "cluster_name" {
  value       = module.eks.cluster_name
  description = "EKS cluster name"
}

output "cluster_endpoint" {
  value       = module.eks.cluster_endpoint
  description = "EKS API server endpoint (used by Helm and kubectl providers)"
}

output "cluster_version" {
  value       = module.eks.cluster_version
  description = "Kubernetes version running on the EKS control plane"
}

output "configure_kubectl" {
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
  description = "Run this command locally to configure kubectl after cluster creation"
}

# ── S3 — pipeline data spine ──────────────────────────────────────────────
output "s3_scripts_bucket" {
  value       = aws_s3_bucket.scripts.bucket
  description = "Bucket name: Agent 1 writes scripts here; Agents 2 & 4 read from it"
}

output "s3_raw_video_bucket" {
  value       = aws_s3_bucket.raw_video.bucket
  description = "Bucket name: Agent 2 writes HeyGen renders; Agent 3 reads from it"
}

output "s3_assets_bucket" {
  value       = aws_s3_bucket.assets.bucket
  description = "Bucket name: static B-roll, music, and background assets"
}

output "s3_final_video_bucket" {
  value       = aws_s3_bucket.final_video.bucket
  description = "Bucket name: Agent 3 writes finished MP4s; Agent 4 reads and uploads to YouTube"
}

# ── IRSA Role ARNs — annotate K8s ServiceAccounts with these in Milestone 2
output "scriptwriter_role_arn" {
  value       = aws_iam_role.scriptwriter.arn
  description = "IRSA ARN for scriptwriter-sa (namespace: default)"
}

output "avatar_director_role_arn" {
  value       = aws_iam_role.avatar_director.arn
  description = "IRSA ARN for avatar-sa (namespace: default)"
}

output "video_editor_role_arn" {
  value       = aws_iam_role.video_editor.arn
  description = "IRSA ARN for video-editor-sa (namespace: video-editor)"
}

output "seo_publisher_role_arn" {
  value       = aws_iam_role.seo_publisher.arn
  description = "IRSA ARN for publisher-sa (namespace: default)"
}

output "orchestrator_role_arn" {
  value       = aws_iam_role.orchestrator.arn
  description = "IRSA ARN for orchestrator-sa (namespace: default)"
}

# ── Karpenter ─────────────────────────────────────────────────────────────
output "karpenter_node_role_name" {
  value       = module.karpenter.node_iam_role_name
  description = "IAM role name that Karpenter-provisioned EC2 nodes assume"
}

output "karpenter_queue_name" {
  value       = module.karpenter.queue_name
  description = "SQS queue name for Karpenter spot interruption handling"
}

# ── Secrets ───────────────────────────────────────────────────────────────
output "secrets_arn" {
  value       = aws_secretsmanager_secret.api_keys.arn
  description = "ARN of the Secrets Manager secret containing all API keys (inject into K8s ExternalSecret or use IRSA to read directly)"
}
