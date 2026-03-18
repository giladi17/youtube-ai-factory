# ── Local: strip https:// prefix from OIDC URL (used in all IRSA policies)
locals {
  oidc_provider = replace(module.eks.cluster_oidc_issuer_url, "https://", "")
}

# ─────────────────────────────────────────────────────────────────────────
# IRSA (IAM Roles for Service Accounts) — one role per agent.
# Each role follows least-privilege: only the S3 paths and secrets it needs.
# K8s manifests (Milestone 2) will annotate each ServiceAccount with its ARN.
# ─────────────────────────────────────────────────────────────────────────

# ── Agent 1: Scriptwriter ─────────────────────────────────────────────────
# Needs: write to yt-scripts, read OpenAI secret
resource "aws_iam_role" "scriptwriter" {
  name = "${var.cluster_name}-scriptwriter-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:default:scriptwriter-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scriptwriter" {
  name = "scriptwriter-policy"
  role = aws_iam_role.scriptwriter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteScripts"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.scripts.arn}/*"
      },
      {
        Sid      = "ListScripts"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.scripts.arn
      },
      {
        Sid      = "ReadApiKeys"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.api_keys.arn
      }
    ]
  })
}

# ── Agent 2: Avatar Director ──────────────────────────────────────────────
# Needs: read yt-scripts, write yt-raw-video, read HeyGen secret
resource "aws_iam_role" "avatar_director" {
  name = "${var.cluster_name}-avatar-director-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:default:avatar-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "avatar_director" {
  name = "avatar-director-policy"
  role = aws_iam_role.avatar_director.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadScripts"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.scripts.arn}/*"
      },
      {
        Sid      = "WriteRawVideo"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.raw_video.arn}/*"
      },
      {
        Sid      = "ReadApiKeys"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.api_keys.arn
      }
    ]
  })
}

# ── Agent 3: Video Editor ─────────────────────────────────────────────────
# Needs: read yt-scripts + yt-raw-video + yt-assets, write yt-final-video
# No secrets needed — purely S3-to-S3 with local FFmpeg processing
# ServiceAccount lives in the video-editor namespace
resource "aws_iam_role" "video_editor" {
  name = "${var.cluster_name}-video-editor-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:video-editor:video-editor-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "video_editor" {
  name = "video-editor-policy"
  role = aws_iam_role.video_editor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadInputBuckets"
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.scripts.arn}/*",
          "${aws_s3_bucket.raw_video.arn}/*",
          "${aws_s3_bucket.assets.arn}/*"
        ]
      },
      {
        Sid    = "ListAssets"
        Effect = "Allow"
        Action = "s3:ListBucket"
        Resource = [
          aws_s3_bucket.raw_video.arn,
          aws_s3_bucket.assets.arn
        ]
      },
      {
        Sid      = "WriteFinalVideo"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.final_video.arn}/*"
      }
    ]
  })
}

# ── Agent 4: SEO Publisher ────────────────────────────────────────────────
# Needs: read yt-scripts + yt-final-video, read YouTube/OpenAI secrets
resource "aws_iam_role" "seo_publisher" {
  name = "${var.cluster_name}-seo-publisher-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:default:publisher-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "seo_publisher" {
  name = "seo-publisher-policy"
  role = aws_iam_role.seo_publisher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadPipelineOutputs"
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.scripts.arn}/*",
          "${aws_s3_bucket.final_video.arn}/*"
        ]
      },
      {
        Sid      = "ReadApiKeys"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.api_keys.arn
      }
    ]
  })
}

# ── Pipeline Orchestrator ─────────────────────────────────────────────────
# Needs: read secrets to inject into spawned jobs (no direct S3 access)
resource "aws_iam_role" "orchestrator" {
  name = "${var.cluster_name}-orchestrator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:default:orchestrator-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "orchestrator" {
  name = "orchestrator-policy"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadApiKeys"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.api_keys.arn
      }
    ]
  })
}
