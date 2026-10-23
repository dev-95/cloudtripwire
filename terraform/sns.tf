resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  tags = var.tags
}

# AWS emails a confirmation link; nothing is delivered until it is clicked.
resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
