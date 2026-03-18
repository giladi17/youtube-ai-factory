# ── S3 buckets for the 4-stage media pipeline ────────────────────────────
# Bucket names include account ID to guarantee global uniqueness without
# a random suffix (deterministic = easier to reference in agent configs).
data "aws_caller_identity" "current" {}

locals {
  # e.g. "yt-factory-prod-123456789012"
  bucket_suffix = "${var.environment}-${data.aws_caller_identity.current.account_id}"
}

# ── 1. Scripts bucket — Agent 1 writes, Agents 2 & 4 read ────────────────
resource "aws_s3_bucket" "scripts" {
  bucket        = "yt-scripts-${local.bucket_suffix}"
  force_destroy = false # never auto-delete production scripts
  tags          = { Purpose = "pipeline-scripts", Stage = "1-scriptwriter" }
}

resource "aws_s3_bucket_versioning" "scripts" {
  bucket = aws_s3_bucket.scripts.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "scripts" {
  bucket = aws_s3_bucket.scripts.id
  rule {
    id     = "expire-old-scripts"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 90 }
  }
}

# ── 2. Raw video bucket — Agent 2 writes, Agent 3 reads ──────────────────
# Contains the HeyGen avatar renders (green-screen MP4s).
# 7-day TTL: large files, only needed briefly between stages 2 and 3.
resource "aws_s3_bucket" "raw_video" {
  bucket        = "yt-raw-video-${local.bucket_suffix}"
  force_destroy = false
  tags          = { Purpose = "avatar-renders", Stage = "2-avatar-director" }
}

resource "aws_s3_bucket_versioning" "raw_video" {
  bucket = aws_s3_bucket.raw_video.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_video" {
  bucket = aws_s3_bucket.raw_video.id
  rule {
    id     = "expire-raw-video"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 7 }
  }
}

# ── 3. Assets bucket — static media library (permanent) ──────────────────
# B-roll clips, background music tracks, branded backgrounds.
# Versioning enabled so asset updates don't silently break past renders.
resource "aws_s3_bucket" "assets" {
  bucket        = "yt-assets-${local.bucket_suffix}"
  force_destroy = false
  tags          = { Purpose = "static-media-library", Stage = "shared" }
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id
  versioning_configuration { status = "Enabled" }
}

# ── 4. Final video bucket — Agent 3 writes, Agent 4 reads & uploads ──────
# Contains finished 1080p MP4s ready for YouTube.
# 30-day TTL: keep for a month in case re-upload is needed.
resource "aws_s3_bucket" "final_video" {
  bucket        = "yt-final-video-${local.bucket_suffix}"
  force_destroy = false
  tags          = { Purpose = "finished-mp4s", Stage = "3-video-editor" }
}

resource "aws_s3_bucket_versioning" "final_video" {
  bucket = aws_s3_bucket.final_video.id
  versioning_configuration { status = "Disabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "final_video" {
  bucket = aws_s3_bucket.final_video.id
  rule {
    id     = "expire-final-video"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 30 }
  }
}

# ── Block all public access on every bucket ───────────────────────────────
resource "aws_s3_bucket_public_access_block" "all" {
  for_each = {
    scripts     = aws_s3_bucket.scripts.id
    raw_video   = aws_s3_bucket.raw_video.id
    assets      = aws_s3_bucket.assets.id
    final_video = aws_s3_bucket.final_video.id
  }

  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── AES-256 server-side encryption on every bucket ───────────────────────
resource "aws_s3_bucket_server_side_encryption_configuration" "all" {
  for_each = {
    scripts     = aws_s3_bucket.scripts.id
    raw_video   = aws_s3_bucket.raw_video.id
    assets      = aws_s3_bucket.assets.id
    final_video = aws_s3_bucket.final_video.id
  }

  bucket = each.value

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true # reduces SSE request costs ~99%
  }
}
