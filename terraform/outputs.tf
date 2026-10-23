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

locals {
  profile_flag = var.cli_profile != "" ? " --profile ${var.cli_profile}" : ""
  # PowerShell, one line per secret. Read-Host keeps the value out of shell history
  # (history only shows $t), and --value=$t stops a chat id like -100... being read as a flag.
  # bash equivalent: read -rs t; aws ssm put-parameter ... --value="$t" ...; unset t
  ssm_put = "aws ssm put-parameter --type SecureString --overwrite --region ${local.region}${local.profile_flag}"
}

output "ssm_setup_commands" {
  description = "PowerShell: paste one line at a time and type the secret at the prompt. Secrets never enter Terraform state or shell history. If you don't use the default AWS profile, set cli_profile so --profile is included."
  value = local.telegram ? [
    "$t = Read-Host 'Telegram bot token'; ${local.ssm_put} --name ${aws_ssm_parameter.telegram_bot_token[0].name} --value=$t; Remove-Variable t",
    "$t = Read-Host 'Telegram chat id'; ${local.ssm_put} --name ${aws_ssm_parameter.telegram_chat_id[0].name} --value=$t; Remove-Variable t",
  ] : []
}
