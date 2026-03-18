# ── Single Secrets Manager secret — all third-party API credentials ───────
#
# Design decisions:
#   1. One secret object = one IAM GetSecretValue permission statement covers
#      all agents (simpler policy, single rotation target).
#   2. Sensitive TF variables are NEVER stored in tfvars / git.
#      Pass them at apply time or via a gitignored secrets.auto.tfvars file.
#   3. Renewal dates embedded in the secret payload as _RENEWAL_DATE keys
#      so any engineer inspecting the secret immediately sees when to renew.
#      auto_renew = "false" tag is an explicit reminder — no auto-renewals.
#   4. `ignore_changes = [secret_string]` prevents Terraform from overwriting
#      secrets that have been rotated manually outside of Terraform.

resource "aws_secretsmanager_secret" "api_keys" {
  name        = "${var.environment}/yt-factory/api-keys"
  description = "YouTube AI Factory — all third-party API credentials. See _RENEWAL_DATE keys inside for subscription management."

  # 7-day recovery window: accidental deletes can be recovered within a week
  recovery_window_in_days = 7

  tags = {
    Purpose   = "api-credentials"
    AutoRenew = "false" # FinOps control: review all subscriptions before renewing
  }
}

resource "aws_secretsmanager_secret_version" "api_keys" {
  secret_id = aws_secretsmanager_secret.api_keys.id

  secret_string = jsonencode({
    # ── AI / Content Generation ───────────────────────────────────────
    OPENAI_API_KEY = var.openai_api_key
    # _OPENAI_RENEWAL_DATE: N/A — pay-as-you-go, no subscription

    HEYGEN_API_KEY       = var.heygen_api_key
    _HEYGEN_RENEWAL_DATE = var.heygen_renewal_date # e.g. "2027-03-01"

    # ── YouTube Data API v3 (OAuth2) ───────────────────────────────────
    # Client ID + Secret: from Google Cloud Console → Credentials
    # Refresh Token:      obtained once via OAuth2 consent flow, long-lived
    YOUTUBE_CLIENT_ID      = var.youtube_client_id
    YOUTUBE_CLIENT_SECRET  = var.youtube_client_secret
    YOUTUBE_REFRESH_TOKEN  = var.youtube_refresh_token
    _YOUTUBE_RENEWAL_DATE  = "N/A — free quota tier, no subscription"

    # ── Container Registry ────────────────────────────────────────────
    DOCKERHUB_USERNAME = var.dockerhub_username
    DOCKERHUB_TOKEN    = var.dockerhub_token
  })

  # Prevents Terraform from overwriting manually-rotated secrets.
  # To force a full refresh: terraform taint aws_secretsmanager_secret_version.api_keys
  lifecycle {
    ignore_changes = [secret_string]
  }
}
