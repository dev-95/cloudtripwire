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

# --- Kill switch Lambda ---

resource "aws_iam_role" "killswitch" {
  name               = "${local.name}-killswitch"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "killswitch" {
  # Describe calls don't support resource-level permissions; both read-only.
  statement {
    sid       = "ReadOnlyDiscovery"
    actions   = ["ec2:DescribeRegions", "ec2:DescribeInstances"]
    resources = ["*"]
  }

  # Find the key's user (Health events don't name it), then set the key Inactive.
  # Never iam:DeleteAccessKey. Users can have paths; user/* matches them.
  statement {
    sid       = "DeactivateLeakedKey"
    actions   = ["iam:GetAccessKeyLastUsed", "iam:UpdateAccessKey"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:user/*"]
  }

  # Same guard as the TTL role: IAM refuses to stop or tag tripwire=ignore instances.
  statement {
    sid       = "StopUnlessIgnored"
    actions   = ["ec2:StopInstances"]
    resources = ["arn:${local.partition}:ec2:*:${local.account_id}:instance/*"]
    condition {
      test     = "StringNotEqualsIgnoreCase"
      variable = "aws:ResourceTag/tripwire"
      values   = ["ignore"]
    }
  }

  # Only the tripwire:quarantined tag. Null check: ForAllValues is true for no keys.
  statement {
    sid       = "TagQuarantinedUnlessIgnored"
    actions   = ["ec2:CreateTags"]
    resources = ["arn:${local.partition}:ec2:*:${local.account_id}:instance/*"]
    condition {
      test     = "StringNotEqualsIgnoreCase"
      variable = "aws:ResourceTag/tripwire"
      values   = ["ignore"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["tripwire:quarantined"]
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
    resources = ["${aws_cloudwatch_log_group.killswitch.arn}:*"]
  }

  # Exactly the two parameters notify.py reads (they only exist with the telegram channel).
  statement {
    sid     = "ReadSecrets"
    actions = ["ssm:GetParameter"]
    resources = [
      for p in ["telegram_bot_token", "telegram_chat_id"] :
      "arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_prefix}/${p}"
    ]
  }

  # Action log only (state.log_action).
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

resource "aws_iam_role_policy" "killswitch" {
  name   = "${local.name}-killswitch"
  role   = aws_iam_role.killswitch.id
  policy = data.aws_iam_policy_document.killswitch.json
}

# --- Kill switch forwarder (only when the home region isn't us-east-1) ---
# Lets the us-east-1 rules put matched events on the home region's default bus.

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "killswitch_forward" {
  count              = local.forward ? 1 : 0
  name               = "${local.name}-killswitch-forward"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "killswitch_forward" {
  statement {
    sid       = "ForwardToHomeBus"
    actions   = ["events:PutEvents"]
    resources = [local.home_bus_arn]
  }
}

resource "aws_iam_role_policy" "killswitch_forward" {
  count  = local.forward ? 1 : 0
  name   = "${local.name}-killswitch-forward"
  role   = aws_iam_role.killswitch_forward[0].id
  policy = data.aws_iam_policy_document.killswitch_forward.json
}
