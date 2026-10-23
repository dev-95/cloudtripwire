# Zips src/ so the tripwire package sits at the zip root (handler tripwire.sweep.handler).
# Only stdlib + boto3 from the Lambda runtime are used, so there is nothing else to bundle.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/cloudtripwire.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
  # Same file modes on Windows and Linux, so the hash (and the deploy) only changes with the code.
  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "sweep" {
  name              = "/aws/lambda/${local.name}-sweep"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_lambda_function" "sweep" {
  function_name    = "${local.name}-sweep"
  description      = "CloudTripwire daily zombie sweep"
  role             = aws_iam_role.sweep.arn
  runtime          = "python3.12"
  handler          = "tripwire.sweep.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      MODE                  = var.mode
      REGIONS               = join(",", var.regions)
      SNAPSHOT_MAX_AGE_DAYS = tostring(var.snapshot_max_age_days)
      STOPPED_INSTANCE_DAYS = tostring(var.stopped_instance_days)
      NAT_IDLE_DAYS         = tostring(var.nat_idle_days)
      CHANNELS              = join(",", var.channels)
      ALERT_EMAIL           = var.alert_email
      STATE_TABLE           = aws_dynamodb_table.state.name
      ALERT_TOPIC_ARN       = aws_sns_topic.alerts.arn
    }
  }

  tags = var.tags

  # The log group must exist first, or Lambda would create one with no retention.
  depends_on = [aws_cloudwatch_log_group.sweep, aws_iam_role_policy.sweep]

  lifecycle {
    precondition {
      condition     = !contains(var.channels, "email") || var.alert_email != ""
      error_message = "alert_email is required when channels includes \"email\"."
    }
  }
}
