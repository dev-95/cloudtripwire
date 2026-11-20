# Permissions

Every IAM permission CloudTripwire uses, and why. This file must match
[`terraform/iam.tf`](../terraform/iam.tf).

Rules for every role:

- One role per Lambda, inline policy only (no AWS managed policies).
- No `Delete*`, `Terminate*`, `Release*`, and no wildcard **actions**.
- `Resource: "*"` only where AWS requires it: Describe calls and
  `cloudwatch:GetMetricStatistics` don't support resource-level permissions. All of
  them are read-only.

## Sweep Lambda (`cloudtripwire-sweep`)

### Read-only discovery (`Resource: "*"`)

| Action | Why |
|---|---|
| `ec2:DescribeRegions` | List the regions enabled in the account |
| `ec2:DescribeVolumes` | `ebs_unattached`; volume sizes for `ec2_stopped_long` |
| `ec2:DescribeAddresses` | `eip_unassociated` |
| `ec2:DescribeNatGateways` | `nat_idle` |
| `ec2:DescribeSnapshots` | `snapshots_old` |
| `ec2:DescribeImages` | `snapshots_old`: skip snapshots used by your AMIs |
| `ec2:DescribeInstances` | `ec2_stopped_long` |
| `elasticloadbalancing:DescribeLoadBalancers` | `elb_no_targets` |
| `elasticloadbalancing:DescribeTargetGroups` | `elb_no_targets`: which target groups belong to which LB |
| `elasticloadbalancing:DescribeTargetHealth` | `elb_no_targets`: count registered targets |
| `elasticloadbalancing:DescribeTags` | `elb_no_targets`: honour `tripwire=ignore` (ELBv2 tags aren't in DescribeLoadBalancers) |
| `cloudwatch:GetMetricStatistics` | `nat_idle`: NAT traffic over the lookback window |

### Scoped to CloudTripwire's own resources

| Action | Resource | Why |
|---|---|---|
| `logs:CreateLogStream`, `logs:PutLogEvents` | `/aws/lambda/cloudtripwire-sweep` log group | Write its own logs. The group is created by Terraform (30-day retention), so `logs:CreateLogGroup` isn't granted. |
| `ssm:GetParameter` | `parameter/cloudtripwire/*` | Read the Telegram bot token and chat id |
| `dynamodb:UpdateItem`, `dynamodb:Scan` | the `cloudtripwire-state` table | Record findings (upsert keeping `first_seen`), find open findings, mark them resolved. Exactly the calls the sweep makes. |
| `sns:Publish` | the `cloudtripwire-alerts` topic | Email alerts |

### Not granted

- Any write to EC2, ELB, RDS, or IAM. The sweep only reports; it never changes
  your resources in any mode.
- `kms:Decrypt`: the SecureString parameters use the AWS managed `aws/ssm` key,
  whose key policy already lets principals in the account decrypt through SSM.
  Verified 2026-10-24 in the test account: with the statement removed,
  `ssm:GetParameter` with decryption still worked and Telegram sent. If you
  move the parameters to a customer managed key, grant `kms:Decrypt` on that
  key with `kms:ViaService = ssm.<region>.amazonaws.com`.
- `dynamodb:PutItem` / `GetItem` / `DeleteItem`: the sweep doesn't use them
  (the TTL and kill switch Lambdas have `PutItem` for the action log and alert
  markers).

## TTL Lambda (`cloudtripwire-ttl`)

### Read-only discovery (`Resource: "*"`)

| Action | Why |
|---|---|
| `ec2:DescribeRegions` | List the regions enabled in the account |
| `ec2:DescribeInstances` | Running instances, their `ttl` tags, launch time, Spot/ASG status |
| `rds:DescribeDBInstances` | Available DB instances, their tags (`TagList` is in the response), create time, replica/cluster status |

Not needed: `rds:ListTagsForResource`. `DescribeDBInstances` already returns
each instance's `TagList` (RDS API reference, `DBInstance`, checked 2026-10-24).

### Writes (enforce mode only)

Resources: `arn:aws:ec2:*:<account>:instance/*` and `arn:aws:rds:*:<account>:db:*`
(every region, because the TTL Lambda checks every enabled region).

| Action | Conditions | Why |
|---|---|---|
| `ec2:StopInstances`, `rds:StopDBInstance` | not tagged `tripwire=ignore` | Stop a resource whose ttl expired. Never `TerminateInstances` / `DeleteDBInstance`. |
| `ec2:CreateTags`, `rds:AddTagsToResource` | not tagged `tripwire=ignore`; the request may only set the tag key `tripwire:stopped-at` | Record when it was stopped. It can't add, change or remove any other tag (so it can't remove your `tripwire=ignore`). |

How the conditions work (`terraform/iam.tf`):

- `StringNotEqualsIgnoreCase` on `aws:ResourceTag/tripwire` = `ignore`: IAM itself
  refuses to stop or tag an ignored resource, even if the code had a bug. The code
  checks the tag too (`tags.is_ignored`), so in normal operation IAM never has to
  refuse. A negated operator is true when the tag is missing, so untagged resources
  are allowed.
- `ForAllValues:StringEquals` on `aws:TagKeys` = `tripwire:stopped-at`, plus `Null`
  `aws:TagKeys` = `false`: every tag key in the request must be
  `tripwire:stopped-at`. The `Null` check matters because `ForAllValues` is true
  when the request has no tag keys at all.
- RDS supports `aws:ResourceTag`, `aws:RequestTag` and `aws:TagKeys` for DB
  instances (RDS User Guide, "How Amazon RDS works with IAM", ABAC: Yes; checked
  2026-10-24). EC2 supports them on instances.

### Scoped to CloudTripwire's own resources

| Action | Resource | Why |
|---|---|---|
| `logs:CreateLogStream`, `logs:PutLogEvents` | `/aws/lambda/cloudtripwire-ttl` log group | Its own logs |
| `ssm:GetParameter` | `parameter/cloudtripwire/*` | Telegram token and chat id |
| `dynamodb:PutItem` | the `cloudtripwire-state` table | Action log and alert-once markers (`state.log_action`, `state.should_alert`). The only DynamoDB call it makes. |
| `sns:Publish` | the `cloudtripwire-alerts` topic | Email alerts |

## Kill switch Lambda (`cloudtripwire-killswitch`)

### Read-only discovery (`Resource: "*"`)

| Action | Why |
|---|---|
| `ec2:DescribeRegions` | List the regions enabled in the account |
| `ec2:DescribeInstances` | Instances launched within `killswitch_lookback_minutes`, their tags, Spot/ASG status |

### Writes (enforce mode only)

| Action | Resource | Conditions | Why |
|---|---|---|---|
| `iam:GetAccessKeyLastUsed` | `arn:aws:iam::<account>:user/*` | | Find which user owns the leaked key. AWS Health events only carry the key ID. Read-only, but scoped to users anyway. |
| `iam:UpdateAccessKey` | `arn:aws:iam::<account>:user/*` | | Set the leaked key `Inactive` (undo: `--status Active`). Never `iam:DeleteAccessKey`. |
| `ec2:StopInstances` | `arn:aws:ec2:*:<account>:instance/*` | not tagged `tripwire=ignore` | Stop instances launched in the lookback window (any region) |
| `ec2:CreateTags` | same | not tagged `tripwire=ignore`; may only set `tripwire:quarantined` | Mark what the kill switch stopped |

The conditions work exactly as for the TTL role (above).
Not needed: `iam:ListAccessKeys` (it needs the user name, which Health events don't
give; `GetAccessKeyLastUsed` returns it). Approved 2026-11-19.

### Scoped to CloudTripwire's own resources

| Action | Resource | Why |
|---|---|---|
| `logs:CreateLogStream`, `logs:PutLogEvents` | `/aws/lambda/cloudtripwire-killswitch` log group | Its own logs |
| `ssm:GetParameter` | exactly `parameter/cloudtripwire/telegram_bot_token` and `.../telegram_chat_id` | Telegram token and chat id |
| `dynamodb:PutItem` | the `cloudtripwire-state` table | Action log with undo commands (`state.log_action`) |
| `sns:Publish` | the `cloudtripwire-alerts` topic | Email alerts |

## Kill switch forwarder role (`cloudtripwire-killswitch-forward`)

Only created when you deploy outside us-east-1. Assumed by EventBridge
(`events.amazonaws.com`, condition `aws:SourceAccount` = your account) for the two
us-east-1 forwarding rules.

| Action | Resource | Why |
|---|---|---|
| `events:PutEvents` | the default event bus in your home region | Forward AWS Health exposed-key events (which AWS sends to us-east-1 only) and us-east-1 GuardDuty findings to the kill switch |

## Other resources

- **CloudWatch alarms** `cloudtripwire-sweep-errors` and `cloudtripwire-ttl-errors`
  publish to the alerts topic if that Lambda errors or times out (at most once a
  day each). `cloudtripwire-killswitch-errors` uses a 5-minute period, because a
  kill switch crash needs attention right away. This uses the topic's default
  access policy (same account); no extra IAM.
- **EventBridge rules** `cloudtripwire-sweep`, `cloudtripwire-ttl`,
  `cloudtripwire-killswitch-health` and `cloudtripwire-killswitch-guardduty` invoke
  their Lambda through a resource-based permission (`lambda:InvokeFunction` for
  `events.amazonaws.com`, limited to that rule's ARN).
