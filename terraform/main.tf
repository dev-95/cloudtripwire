# CloudTripwire module: sweep path (daily zombie scan -> DynamoDB -> Telegram/email)
# and TTL path (hourly ttl-tag check -> stop in enforce mode -> Telegram/email).
# Region and account come from the caller's provider; nothing is hard-coded.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  name       = "cloudtripwire"
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition

  ssm_prefix = "/cloudtripwire"
  telegram   = contains(var.channels, "telegram")
}

# Warning only (check blocks never fail plan/apply): the sweep-errors alarm goes to the
# SNS topic, which only delivers email, so Telegram-only users won't hear about a crash.
check "crash_alerts_reachable" {
  assert {
    condition     = var.alert_email != ""
    error_message = "Crash alerts are delivered by email only; set alert_email."
  }
}
