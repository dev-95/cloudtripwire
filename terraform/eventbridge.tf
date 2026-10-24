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
