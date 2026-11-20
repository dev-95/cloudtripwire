resource "aws_cloudwatch_event_rule" "sweep" {
  name                = "${local.name}-sweep"
  description         = "CloudTripwire daily zombie sweep"
  schedule_expression = var.sweep_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "sweep" {
  rule = aws_cloudwatch_event_rule.sweep.name
  arn  = aws_lambda_function.sweep.arn
}

resource "aws_lambda_permission" "sweep_schedule" {
  statement_id  = "AllowEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sweep.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweep.arn
}

# If the sweep crashes before it can alert (config, region list, DynamoDB), the user
# still hears about it. Lambda timeouts count as Errors too.
resource "aws_cloudwatch_metric_alarm" "sweep_errors" {
  alarm_name          = "${local.name}-sweep-errors"
  alarm_description   = "CloudTripwire sweep Lambda failed. Check its CloudWatch logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.sweep.function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_event_rule" "ttl" {
  name                = "${local.name}-ttl"
  description         = "CloudTripwire TTL enforcement"
  schedule_expression = var.ttl_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "ttl" {
  rule = aws_cloudwatch_event_rule.ttl.name
  arn  = aws_lambda_function.ttl.arn
}

resource "aws_lambda_permission" "ttl_schedule" {
  statement_id  = "AllowEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ttl.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ttl.arn
}

# --- Kill switch ---
# Event shapes and sources: docs/killswitch.md. AWS Health sends global events (IAM,
# exposed keys) to us-east-1 only, so a us-east-1 rule forwards them to the home
# region's default bus, where the same pattern invokes the Lambda. GuardDuty findings
# are regional: home-region findings match directly, us-east-1 findings are forwarded.
# When the home region is us-east-1, nothing is forwarded.

locals {
  health_pattern = jsonencode({
    source        = ["aws.health"]
    "detail-type" = ["AWS Health Event"]
    detail        = { eventTypeCode = ["AWS_RISK_CREDENTIALS_EXPOSED"] }
  })
  guardduty_pattern = jsonencode({
    source        = ["aws.guardduty"]
    "detail-type" = ["GuardDuty Finding"]
    detail = {
      type = [
        { prefix = "CryptoCurrency:EC2/" },
        { prefix = "UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration" },
      ]
    }
  })
  killswitch_patterns = {
    health    = local.health_pattern
    guardduty = local.guardduty_pattern
  }
}

resource "aws_cloudwatch_event_rule" "killswitch" {
  for_each      = local.killswitch_patterns
  name          = "${local.name}-killswitch-${each.key}"
  description   = "CloudTripwire kill switch: ${each.key} events"
  event_pattern = each.value
  tags          = var.tags
}

resource "aws_cloudwatch_event_target" "killswitch" {
  for_each = local.killswitch_patterns
  rule     = aws_cloudwatch_event_rule.killswitch[each.key].name
  arn      = aws_lambda_function.killswitch.arn
}

resource "aws_lambda_permission" "killswitch" {
  for_each      = local.killswitch_patterns
  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.killswitch.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.killswitch[each.key].arn
}

resource "aws_cloudwatch_event_rule" "killswitch_forward" {
  for_each      = local.forward ? local.killswitch_patterns : {}
  provider      = aws.us_east_1
  name          = "${local.name}-killswitch-forward-${each.key}"
  description   = "CloudTripwire kill switch: forward ${each.key} events to ${local.region}"
  event_pattern = each.value
  tags          = var.tags
}

resource "aws_cloudwatch_event_target" "killswitch_forward" {
  for_each = local.forward ? local.killswitch_patterns : {}
  provider = aws.us_east_1
  rule     = aws_cloudwatch_event_rule.killswitch_forward[each.key].name
  arn      = local.home_bus_arn
  role_arn = aws_iam_role.killswitch_forward[0].arn
}

# A kill switch crash means a leak goes unanswered: email the alerts topic.
resource "aws_cloudwatch_metric_alarm" "killswitch_errors" {
  alarm_name          = "${local.name}-killswitch-errors"
  alarm_description   = "CloudTripwire kill switch Lambda failed. Check its CloudWatch logs now."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.killswitch.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Same as the sweep alarm. One day period: an hourly crash alerts once a day, not 24 times.
resource "aws_cloudwatch_metric_alarm" "ttl_errors" {
  alarm_name          = "${local.name}-ttl-errors"
  alarm_description   = "CloudTripwire TTL Lambda failed. Check its CloudWatch logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.ttl.function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}
