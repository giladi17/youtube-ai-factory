# ── FinOps guard rails ────────────────────────────────────────────────────
#
# AWS CloudWatch Billing alarms must reside in us-east-1 — that is the only
# region where the AWS/Billing namespace is published, regardless of where
# workloads run.  The `aws.us_east_1` provider alias is defined in providers.tf.
#
# Two thresholds:
#   $20 — soft warning (expected monthly cost for a low-volume pipeline)
#   $50 — hard warning (investigate immediately; something is mis-configured)

# ── SNS Topic (us-east-1) — receives billing alarm notifications ──────────
resource "aws_sns_topic" "billing_alerts" {
  provider = aws.us_east_1
  name     = "yt-factory-billing-alerts"
}

resource "aws_sns_topic_subscription" "billing_email" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.billing_alerts.arn
  protocol  = "email"
  endpoint  = var.billing_alert_email
  # After apply: AWS sends a confirmation email — subscriber must click the
  # confirmation link before alerts are delivered.
}

# ── Alarm #1 — $20/month soft threshold ──────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "billing_20" {
  provider = aws.us_east_1

  alarm_name          = "yt-factory-billing-20usd"
  alarm_description   = "YouTube AI Factory: estimated monthly AWS charges exceeded $20 USD. Review Karpenter node activity and S3 storage usage."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 86400 # evaluated once per day (billing metrics cadence)
  statistic           = "Maximum"
  threshold           = 20
  treat_missing_data  = "notBreaching"

  dimensions = {
    Currency = "USD"
  }

  alarm_actions = [aws_sns_topic.billing_alerts.arn]
  ok_actions    = [aws_sns_topic.billing_alerts.arn]

  tags = { Severity = "warning" }
}

# ── Alarm #2 — $50/month hard threshold ──────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "billing_50" {
  provider = aws.us_east_1

  alarm_name          = "yt-factory-billing-50usd-URGENT"
  alarm_description   = "YouTube AI Factory: estimated monthly AWS charges exceeded $50 USD. IMMEDIATE ACTION REQUIRED. Check for runaway Karpenter nodes or accidental large S3 uploads."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 86400
  statistic           = "Maximum"
  threshold           = 50
  treat_missing_data  = "notBreaching"

  dimensions = {
    Currency = "USD"
  }

  alarm_actions = [aws_sns_topic.billing_alerts.arn]

  tags = { Severity = "critical" }
}
