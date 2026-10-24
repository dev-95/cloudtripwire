# One role per Lambda, least privilege. Every action and its reason: docs/permissions.md.
# No Delete*/Terminate*/Release* and no wildcard actions anywhere (CI checks this).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sweep" {
  name               = "${local.name}-sweep"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "sweep" {
  # Describe/List/Get-metric calls don't support resource-level permissions, so AWS
  # requires Resource "*". They are all read-only.
  statement {
    sid = "ReadOnlyDiscovery"
    actions = [
      "ec2:DescribeRegions",
      "ec2:DescribeVolumes",
      "ec2:DescribeAddresses",
      "ec2:DescribeNatGateways",
      "ec2:DescribeSnapshots",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
      "elasticloadbalancing:DescribeTags",
      "cloudwatch:GetMetricStatistics",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.sweep.arn}:*"]
  }

  statement {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
  }

  # Exactly what common/state.py uses on the sweep path.
  statement {
    sid       = "StateTable"
    actions   = ["dynamodb:UpdateItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "sweep" {
  name   = "${local.name}-sweep"
  role   = aws_iam_role.sweep.id
  policy = data.aws_iam_policy_document.sweep.json
}

# --- TTL Lambda ---

resource "aws_iam_role" "ttl" {
  name               = "${local.name}-ttl"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

locals {
  # The TTL Lambda works in every enabled region, so the region part is "*".
  ttl_targets = [
    "arn:${local.partition}:ec2:*:${local.account_id}:instance/*",
    "arn:${local.partition}:rds:*:${local.account_id}:db:*",
  ]
}

data "aws_iam_policy_document" "ttl" {
  # Describe calls don't support resource-level permissions; all read-only.
  # rds:DescribeDBInstances returns TagList, so rds:ListTagsForResource isn't needed.
  statement {
    sid = "ReadOnlyDiscovery"
    actions = [
      "ec2:DescribeRegions",
      "ec2:DescribeInstances",
      "rds:DescribeDBInstances",
    ]
    resources = ["*"]
  }

  # IAM itself refuses to touch tripwire=ignore resources, on top of the code check.
  # A negated operator matches when the tag is absent, so untagged resources are allowed.
  statement {
    sid       = "StopUnlessIgnored"
    actions   = ["ec2:StopInstances", "rds:StopDBInstance"]
    resources = local.ttl_targets
    condition {
      test     = "StringNotEqualsIgnoreCase"
      variable = "aws:ResourceTag/tripwire"
      values   = ["ignore"]
    }
  }

  # Only the tripwire:stopped-at tag, only on resources not tagged tripwire=ignore.
  # ForAllValues is true for an empty key, so the Null check requires tag keys in the request.
  statement {
    sid       = "TagStoppedAtUnlessIgnored"
    actions   = ["ec2:CreateTags", "rds:AddTagsToResource"]
    resources = local.ttl_targets
    condition {
      test     = "StringNotEqualsIgnoreCase"
      variable = "aws:ResourceTag/tripwire"
      values   = ["ignore"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["tripwire:stopped-at"]
    }
    condition {
      test     = "Null"
      variable = "aws:TagKeys"
      values   = ["false"]
    }
  }

  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ttl.arn}:*"]
  }

  statement {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
  }

  # Action log + alert-once markers (state.log_action, state.should_alert).
  statement {
    sid       = "StateTable"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "ttl" {
  name   = "${local.name}-ttl"
  role   = aws_iam_role.ttl.id
  policy = data.aws_iam_policy_document.ttl.json
}
