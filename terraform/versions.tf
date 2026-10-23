terraform {
  required_version = ">= 1.5"

  required_providers {
    # Latest majors on the Terraform Registry as of 2026-10-23: aws 6.70.0, archive 2.8.1.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }
}
