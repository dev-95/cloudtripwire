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
  (the TTL and kill switch Lambdas will get `PutItem` for the action log and
  alert markers).

## Other resources

- **CloudWatch alarm** `cloudtripwire-sweep-errors` publishes to the alerts topic
  if the sweep Lambda errors or times out. This uses the topic's default access
  policy (same account); no extra IAM.
- **EventBridge rule** `cloudtripwire-sweep` invokes the Lambda through a
  resource-based permission (`lambda:InvokeFunction` for `events.amazonaws.com`,
  limited to that rule's ARN).
