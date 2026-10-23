# Findings, action log, alert-once markers. Layout: docs/architecture.md.
# Point-in-time recovery is off on purpose: it costs money and this table only holds
# rebuildable history (the next sweep re-detects everything; only first_seen dates reset).
resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # Action log items carry expires_at (epoch seconds, 90 days out); DynamoDB deletes them for free.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = var.tags
}
