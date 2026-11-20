# Minimal install: daily sweep in observe mode, alerts to Telegram.
#   terraform init && terraform apply
#   then run the commands in the ssm_setup_commands output.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  description = "Region to deploy CloudTripwire into. It still scans every enabled region."
  type        = string
  default     = "us-east-1"
}

provider "aws" {
  region = var.region
}

# AWS Health sends exposed-key events to us-east-1; the kill switch listens there.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

module "cloudtripwire" {
  source = "../.."
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  mode     = "observe"
  channels = ["telegram"]
  # channels    = ["telegram", "email"]
  # alert_email = "you@example.com"
}

output "ssm_setup_commands" {
  value = module.cloudtripwire.ssm_setup_commands
}

output "sweep_function_name" {
  value = module.cloudtripwire.sweep_function_name
}
