# CloudTripwire module: sweep path (daily zombie scan -> DynamoDB -> Telegram/email),
# TTL path (hourly ttl-tag check -> stop in enforce mode -> Telegram/email) and
# kill switch (Health/GuardDuty event -> deactivate key, stop new instances -> alert).
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

  # Kill switch: us-east-1 forwards Health (and its GuardDuty) events to the home bus.
  forward      = local.region != "us-east-1"
  home_bus_arn = "arn:${local.partition}:events:${local.region}:${local.account_id}:event-bus/default"
}

# Warning only (check blocks never fail plan/apply): the sweep-errors alarm goes to the
# SNS topic, which only delivers email, so Telegram-only users won't hear about a crash.
check "crash_alerts_reachable" {
  assert {
    condition     = var.alert_email != ""
    error_message = "Crash alerts are delivered by email only; set alert_email."
  }
}
