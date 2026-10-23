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
| `kms:Decrypt` | `key/*` in this account and region, **only** with `kms:ViaService = ssm.<region>.amazonaws.com` | Decrypt those SecureString parameters. The condition means the role can't decrypt anything directly, only through SSM. With the default `aws/ssm` key this may already be allowed by the key policy (not yet verified); it's required if you switch the parameters to a customer managed key. |
| `dynamodb:UpdateItem`, `dynamodb:Scan` | the `cloudtripwire-state` table | Record findings (upsert keeping `first_seen`), find open findings, mark them resolved. Exactly the calls the sweep makes. |
| `sns:Publish` | the `cloudtripwire-alerts` topic | Email alerts |

### Not granted

- Any write to EC2, ELB, RDS, or IAM. The sweep only reports; it never changes
  your resources in any mode.
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
