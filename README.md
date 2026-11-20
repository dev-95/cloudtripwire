# CloudTripwire

[![CI](https://github.com/dev-95/cloudtripwire/actions/workflows/ci.yml/badge.svg)](https://github.com/dev-95/cloudtripwire/actions/workflows/ci.yml)

Install in 2 minutes, stop surprise AWS bills: zombie cleanup, self-destruct timers, and a leaked-key kill switch, running entirely in your own account.

<!-- DEMO GIF: docs/demo.gif (terraform apply -> Telegram digest -> kill switch alert). Add before launch. -->
> Demo GIF coming soon.

v0.1.0 installs with Terraform. A CloudFormation one-click install and Slack alerts come in v0.2.

## The problem

Most surprise AWS bills come from two mistakes. Resources you thought you cleaned up
keep billing quietly: an unattached volume, an idle NAT gateway, a forgotten instance.
Or an access key gets pushed to GitHub, and bots use it within minutes to launch
crypto-mining instances in every region.

## What it does

CloudTripwire is a Terraform module you install into **your own AWS account**. No SaaS,
no data leaves the account, and nobody else ever holds your credentials. It has three
parts:

| Part | Runs | Does |
|---|---|---|
| **Zombie sweep** | daily | Finds forgotten resources in every region, estimates their monthly cost, sends a digest. Report only: it never changes anything. |
| **TTL timers** | hourly | Tag an EC2 or RDS instance `ttl=4h` (or `2d`, `2026-12-01`...) and it gets stopped when time is up. Warns an hour before. |
| **Kill switch** | on AWS's leaked-key alert | Deactivates the leaked key, stops instances launched in the last hour, tells you exactly what it did and how to undo it. |

### Zombie sweep

| Detector | Flags | Estimated cost |
|---|---|---|
| `ebs_unattached` | EBS volumes not attached to any instance | size × per-GB-month price |
| `eip_unassociated` | Elastic IPs not attached to anything | ~$3.65/month each |
| `nat_idle` | NAT gateways that sent under 1 MB in 7 days | ~$32.85/month each |
| `elb_no_targets` | ALBs/NLBs with no registered targets | ~$16.43/month each |
| `snapshots_old` | Your snapshots older than 90 days, not used by an AMI or AWS Backup | size × $0.05/GB-month (upper bound) |
| `ec2_stopped_long` | Instances stopped for more than 7 days | its attached EBS volumes |

Details, cost formulas and suggested fixes: [docs/detectors.md](docs/detectors.md).
Prices are us-east-1 list-price estimates.

A digest looks like this (sample values):

```
🧟 CloudTripwire: 3 zombies found, about $38/month
• NAT gateway nat-0abc1234def567890 (us-east-1): $32.85/mo, NAT gateway sent under 1 MB in the last 7 days (hourly charge only; data processing is extra), flagged for 12 days
• Elastic IP eipalloc-0abc1234def567890 (ca-central-1): $3.65/mo, Elastic IP 203.0.113.25 is not attached to anything, flagged for 30 days
• EBS volume vol-0a1b2c3d4e5f67890 (us-east-1): $1.60/mo, 20 GB gp3 volume, not attached to any instance, flagged for 9 days
Fix commands: in the sweep Lambda's CloudWatch logs.
Mode: report only (the sweep never changes anything)
```

### TTL timers

Tag key `ttl`, value `30m`, `4h`, `2d`, `1w` (from launch) or `2026-12-01` /
`2026-12-01T18:00Z` (UTC). In `observe` mode (the default) you get "would stop"
alerts; in `enforce` mode it stops the instance and sends the restart command.
Instances in Auto Scaling groups, Spot Instances and RDS setups that can't be stopped
are alert only. Optional **lab mode** treats untagged instances as expiring after 24 h,
for dedicated learning accounts. Full rules: [docs/ttl.md](docs/ttl.md).

### Kill switch

AWS watches public code sites for leaked access keys and raises an AWS Health event
when it finds one of yours. The kill switch acts as soon as that event arrives
(about 20 s for a full run in testing). Sample alert, fake values:

```
🚨 CloudTripwire kill switch: leaked key ****TEST
Trigger: AWS Health: key exposed publicly (AWS_RISK_CREDENTIALS_EXPOSED)
Account: 123456789012
• Deactivated key ****TEST (user ci-deploy). Undo (full key ID in the action log): aws iam update-access-key --user-name ci-deploy --access-key-id ****TEST --status Active
• Stopped instance i-0abc1234def567890 in us-east-1. Undo: aws ec2 start-instances --instance-ids i-0abc1234def567890 --region us-east-1
• Stopped instance i-0fed9876cba543210 in eu-west-1. Undo: aws ec2 start-instances --instance-ids i-0fed9876cba543210 --region eu-west-1
Next steps:
1. Rotate the key: create a new one, update whatever used it, then delete the old one.
2. Check CloudTrail Event history, in every region, for what the key did.
3. Look for IAM users, roles, access keys and instances you didn't create.
4. Open an AWS Support case (Account and billing) about charges from the leak.
Mode: enforce
```

With GuardDuty enabled, it also reacts to crypto-mining and credential-exfiltration
findings. Details: [docs/killswitch.md](docs/killswitch.md).

## Install (Terraform, about 2 minutes)

You need Terraform 1.5+, the AWS CLI signed in to the account you want to protect, and
a Telegram bot (or just email).

**1. Add the module.** Create `main.tf` in an empty folder:

```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = "ca-central-1" # your home region; it still scans every enabled region
}

# AWS sends leaked-key events to us-east-1 only; the kill switch listens there.
# Required even if your home region is us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

module "cloudtripwire" {
  source = "github.com/dev-95/cloudtripwire//terraform?ref=v0.1.0"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  channels    = ["telegram", "email"]
  alert_email = "you@example.com" # also receives crash alarms; recommended
}

output "ssm_setup_commands" {
  value = module.cloudtripwire.ssm_setup_commands
}
```

**2. Deploy.**

```
terraform init
terraform apply
```

Confirm the subscription email AWS sends to `alert_email`.

**3. Store your Telegram secrets** (skip if you only use email). Create a bot with
[@BotFather](https://t.me/BotFather) to get its token, send your bot any message,
then open `https://api.telegram.org/bot<token>/getUpdates` and copy `"chat":{"id":...}`.
Run the two PowerShell lines in the `ssm_setup_commands` output, one at a time; each
asks for one value. The values go straight into SSM Parameter Store (SecureString),
never into Terraform state or your shell history. In bash, the equivalent of each
line is:

```bash
read -rs t; aws ssm put-parameter --type SecureString --overwrite --region <home-region> --name /cloudtripwire/telegram_bot_token --value="$t"; unset t
```

**4. Try it.** Run the sweep now instead of waiting for tomorrow:

```
aws lambda invoke --function-name cloudtripwire-sweep --cli-read-timeout 310 out.json
```

If nothing is found you get no message (an all-clear only goes out on Mondays).
`examples/basic` in this repo is a minimal working setup.

## Configuration

All are Terraform variables on the module.

| Variable | Default | Meaning |
|---|---|---|
| `mode` | `"observe"` | `observe` or `enforce` for TTL actions. The sweep only ever reports. |
| `killswitch_mode` | `"enforce"` | `observe` or `enforce` for the kill switch (see [Safety](#safety)) |
| `regions` | `[]` (all enabled) | Limit scanning to these regions |
| `sweep_schedule` | `"cron(0 13 * * ? *)"` | When the daily sweep runs (UTC) |
| `ttl_schedule` | `"rate(1 hour)"` | How often TTL tags are checked |
| `lab_mode` | `false` | Treat running EC2/RDS with no `ttl` tag as expired after `lab_grace_hours` |
| `lab_grace_hours` | `24` | Grace period for lab mode |
| `snapshot_max_age_days` | `90` | Snapshots older than this are flagged |
| `stopped_instance_days` | `7` | Instances stopped longer than this are flagged |
| `nat_idle_days` | `7` | NAT gateway traffic lookback |
| `killswitch_lookback_minutes` | `60` | Instances launched this recently get stopped by the kill switch |
| `channels` | `["telegram"]` | Any of `telegram`, `email` |
| `alert_email` | `""` | Email for alerts and crash alarms (you must confirm the subscription) |
| `cli_profile` | `""` | Adds `--profile` to the `ssm_setup_commands` output |
| `tags` | `{}` | Tags for every CloudTripwire resource |

Outputs: `ssm_setup_commands`, `sweep_function_name`, `ttl_function_name`,
`killswitch_function_name`, `state_table_name`, `alerts_topic_arn`.

## Stop leaks before they happen

The kill switch is the last line of defence. Better to never push a key at all.
[`guardrails/`](guardrails/) has two copy-paste files for **your** repos, both
running [gitleaks](https://github.com/gitleaks/gitleaks):

1. **On your machine:** paste [`guardrails/pre-commit-snippet.yaml`](guardrails/pre-commit-snippet.yaml)
   into your `.pre-commit-config.yaml`, then `pip install pre-commit` and `pre-commit install`.
   Commits containing a secret are refused.
2. **On GitHub:** copy [`guardrails/secret-scan.yml`](guardrails/secret-scan.yml) to
   `.github/workflows/secret-scan.yml`. Every push and pull request is scanned, with a
   pinned, checksum-verified gitleaks.
3. **Already pushed a key?** Deactivate it in IAM now, then rotate it. Removing the
   commit isn't enough: bots have already copied it.

## Safety

- **Stop, never delete.** CloudTripwire never deletes, terminates or releases anything.
  It stops instances, sets keys Inactive and adds its own tags; every action can be
  undone with one command, which is in the alert and the action log. Deleting is your
  call: the sweep shows the command for you to run.
  ([ADR 0001](docs/decisions/0001-stop-not-delete.md))
- **Observe by default.** TTL only reports "would stop" until you set `mode = "enforce"`.
- **The kill switch is the one exception: it defaults to `enforce`.** Leaked keys are
  exploited within minutes, faster than anyone can read an alert, and everything it
  does is reversible. Set `killswitch_mode = "observe"` if you'd rather decide yourself.
  ([ADR 0002](docs/decisions/0002-killswitch-enforce-default.md))
- **Opt out per resource.** Tag anything `tripwire=ignore` and it is never flagged or
  touched. The IAM policies enforce this too, so it holds even if the code had a bug.
- **Least privilege, all documented.** One IAM role per Lambda, no wildcard actions, no
  `Delete*` / `Terminate*` / `Release*` (CI fails if one appears). Every permission and
  why: [docs/permissions.md](docs/permissions.md).
- **No secrets in code or logs.** Tokens live in SSM SecureString parameters; alerts and
  logs show only the last 4 characters of a key ID.
- **No dependencies.** Python standard library + boto3 from the Lambda runtime. Nothing
  to audit beyond this repo.

## Cost

Near zero at normal scale: everything fits the AWS free tier.

- Lambda: about 10 minutes of runtime a day in total (sweep daily, TTL hourly, ~25 s
  each across 17 regions), roughly 5,000 GB-seconds a month against 400,000 free.
- EventBridge schedules and AWS service events: free. DynamoDB on-demand: a handful
  of small writes a day (cents a year at most). SSM standard parameters: free.
  CloudWatch: 3 alarms (10 are free), logs kept 30 days. SNS email: 1,000 free a month.
- **GuardDuty is the only part with a real cost.** It's optional: without it the kill
  switch still reacts to AWS Health leaked-key events. If you enable it, GuardDuty
  bills per event analysed after a 30-day trial.

What CloudTripwire *stops* still bills in part: a stopped instance keeps paying for its
EBS volumes, until you delete them.

## How it was tested

- **252 unit tests** with [moto](https://github.com/getmoto/moto) (no real AWS, no
  network), covering every detector, the TTL rules, the kill switch (including
  "observe makes zero write calls" and "one failing step doesn't stop the others") and
  alert formatting. CI runs them plus ruff, `terraform validate` on Terraform 1.5 and the
  latest 1.x, gitleaks on the full history, and the IAM guard.
- **On real AWS**, in a dedicated test account:
  - **Zombie digest** (2026-10-23): a seeded unattached 1 GB gp3 volume and an unused
    Elastic IP showed up in the Telegram and email digest.
  - **TTL stop** (2026-10-24): a t3.micro tagged `ttl=30m` got the one-hour warning,
    then the scheduled run stopped it, tagged it and logged the undo command.
  - **Kill switch** (2026-11-20): a canary IAM user's key plus a t3.micro launched a
    minute earlier. A simulated AWS Health event deactivated the key and stopped and
    tagged the instance (17 regions checked in 19 s), with both alerts sent.
  - **Cross-region forwarding** (2026-11-20): a custom test event put on the us-east-1
    bus reached the kill switch in ca-central-1 within 15 s, source intact.
- **Not yet tested live:** a real AWS Health leaked-key event (AWS doesn't let anyone
  send those by hand), GuardDuty findings, and stopping RDS (tested in moto only).

## FAQ

**Does the kill switch work on the AWS Free plan?**
Yes, for leaked keys: AWS Health reports exposed access keys on every account, and the
kill switch reacts to that. The second trigger, GuardDuty findings (crypto mining,
credential exfiltration), needs GuardDuty enabled. GuardDuty isn't available on the
Free account plan and costs money after its 30-day trial on a paid plan.

**Will GuardDuty sample findings trigger the kill switch?**
No. Sample findings (from "Generate sample findings" or `aws guardduty create-sample-findings`)
only send a "sample finding received, no action taken" alert. They never deactivate a key
or stop an instance, in any mode. See [docs/killswitch.md](docs/killswitch.md).

**Why does it need a us-east-1 provider?**
AWS Health sends account-wide events, including leaked keys, to us-east-1 only. The
module puts a small forwarding rule there that passes them to your home region.

**Can it see my data?**
It reads resource metadata (sizes, tags, states, traffic totals), never the contents of
volumes, snapshots or databases. And it all runs in your account; nothing is sent
anywhere except your own alerts.

**I installed it. How do I tell you?**
Star the repo or say hi in GitHub Discussions. There is no telemetry, ever.

## Uninstall

```
terraform destroy
```

This removes everything CloudTripwire created: Lambdas, roles, rules, alarms, the SNS
topic, the SSM parameters and the state table (with the action log; to keep it, first run
`aws dynamodb scan --table-name cloudtripwire-state > cloudtripwire-actions.json`).
It doesn't touch your resources: anything it stopped stays stopped, and any key it
deactivated stays Inactive, until you restart or re-enable them.

## Lessons learned

Things that only showed up on real AWS:

- **Pasted secrets keep their quotes.** Telegram failed four different ways on the first
  deploy (404, 400, 401, "chat not found"). Two were a token and chat ID stored with
  surrounding quotes from a PowerShell paste. The setup commands now trim them, and the
  Telegram error description is logged (token scrubbed) so the next problem is obvious.
- **A permission that looked necessary wasn't.** `kms:Decrypt` for SSM SecureStrings was
  in the first policy. With it removed, the live test still decrypted fine: the AWS-managed
  `aws/ssm` key already allows it through SSM. One fewer permission.
- **Leaked-key events only arrive in us-east-1.** AWS Health sends global events there
  regardless of where you deploy, so the module forwards them across regions with an
  EventBridge rule instead of asking everyone to deploy in us-east-1.
- **The AWS Free plan blocks GuardDuty.** The planned forwarding test used GuardDuty sample
  findings and failed with `SubscriptionRequiredException`. Forwarding was proven instead
  with a temporary custom-source event, which needs nothing enabled.
- **Live calls are slower than tests.** A Telegram send during the kill switch demo took
  about 10 s, right at the 10 s timeout, so it was raised to 15 s.

## License

MIT
