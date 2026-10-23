# Placeholders only. The real values are set by the user with `aws ssm put-parameter`
# (see the ssm_setup_commands output), and ignore_changes keeps Terraform from reading
# them back or overwriting them, so real secrets never enter Terraform state.
resource "aws_ssm_parameter" "telegram_bot_token" {
  count = local.telegram ? 1 : 0

  name        = "${local.ssm_prefix}/telegram_bot_token"
  description = "CloudTripwire Telegram bot token (set manually)"
  type        = "SecureString"
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "telegram_chat_id" {
  count = local.telegram ? 1 : 0

  name        = "${local.ssm_prefix}/telegram_chat_id"
  description = "CloudTripwire Telegram chat id (set manually)"
  type        = "SecureString"
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}
