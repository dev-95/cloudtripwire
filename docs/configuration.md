# Configuration

Full install steps, every variable, cost, FAQ and uninstall. Back to the
[README](../README.md).

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
never into Terraform state or your shell history. In bash, the equivalent of the two
lines is:

```bash
read -rs t; aws ssm put-parameter --type SecureString --overwrite --region <home-region> --name /cloudtripwire/telegram_bot_token --value="$t"; unset t
read -rs t; aws ssm put-parameter --type SecureString --overwrite --region <home-region> --name /cloudtripwire/telegram_chat_id --value="$t"; unset t
```

**4. Try it.** Run the sweep now instead of waiting for tomorrow:

```
aws lambda invoke --function-name cloudtripwire-sweep --cli-read-timeout 310 out.json
```

If nothing is found you get no message (an all-clear only goes out on Mondays).
`examples/basic` in this repo is a minimal working setup.

## Variables

All are Terraform variables on the module.

| Variable | Default | Meaning |
|---|---|---|
| `mode` | `"observe"` | `observe` or `enforce` for TTL actions. The sweep only ever reports. |
| `killswitch_mode` | `"enforce"` | `observe` or `enforce` for the kill switch (why: [ADR 0002](decisions/0002-killswitch-enforce-default.md)) |
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

## Outputs

`ssm_setup_commands`, `sweep_function_name`, `ttl_function_name`,
`killswitch_function_name`, `state_table_name`, `alerts_topic_arn`.

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

## FAQ

**Does the kill switch work on the AWS Free plan?**
Yes, for leaked keys: AWS Health reports exposed access keys on every account, and the
kill switch reacts to that. The second trigger, GuardDuty findings (crypto mining,
credential exfiltration), needs GuardDuty enabled. GuardDuty isn't available on the
Free account plan and costs money after its 30-day trial on a paid plan.

**Will GuardDuty sample findings trigger the kill switch?**
No. Sample findings (from "Generate sample findings" or `aws guardduty create-sample-findings`)
only send a "sample finding received, no action taken" alert. They never deactivate a key
or stop an instance, in any mode. See [killswitch.md](killswitch.md).

**Why does it need a us-east-1 provider?**
AWS Health sends account-wide events, including leaked keys, to us-east-1 only. The
module puts two small forwarding rules there (one for AWS Health, one for GuardDuty
findings in us-east-1) that pass them to your home region.

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
