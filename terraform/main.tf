# CloudTripwire module: sweep path (daily zombie scan -> DynamoDB -> Telegram/email).
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
