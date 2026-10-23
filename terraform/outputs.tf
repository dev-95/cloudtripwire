output "sweep_function_name" {
  description = "Sweep Lambda name. Run it now: aws lambda invoke --function-name <name> out.json"
  value       = aws_lambda_function.sweep.function_name
}

output "state_table_name" {
  description = "DynamoDB table holding findings, the action log and alert markers."
  value       = aws_dynamodb_table.state.name
}

output "alerts_topic_arn" {
  description = "SNS topic for email alerts and the sweep error alarm."
  value       = aws_sns_topic.alerts.arn
}

output "ssm_setup_commands" {
  description = "Run these once to store your Telegram secrets (they never enter Terraform state). Tip: the value lands in your shell history; clear it afterwards."
  value = local.telegram ? [
    "aws ssm put-parameter --name ${aws_ssm_parameter.telegram_bot_token[0].name} --type SecureString --overwrite --value \"<your-bot-token>\" --region ${local.region}",
    "aws ssm put-parameter --name ${aws_ssm_parameter.telegram_chat_id[0].name} --type SecureString --overwrite --value \"<your-chat-id>\" --region ${local.region}",
  ] : []
}
