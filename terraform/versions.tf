terraform {
  required_version = ">= 1.5"

  required_providers {
    # Latest majors on the Terraform Registry as of 2026-10-23: aws 6.70.0, archive 2.8.1.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
      # us-east-1 receives AWS Health global events (exposed keys); the kill switch
      # forwards them from there. Pass it even when you deploy in us-east-1.
      configuration_aliases = [aws.us_east_1]
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }
}
