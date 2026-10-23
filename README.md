# CloudTripwire

Install in 2 minutes, stop surprise AWS bills: zombie cleanup, self-destruct timers, and a leaked-key kill switch, running entirely in your own account.

> Work in progress. Not ready to install yet.

## What it detects

| Detector | Flags | Estimated cost |
|---|---|---|
| `ebs_unattached` | EBS volumes not attached to any instance | size × per-GB-month price |
| `eip_unassociated` | Elastic IPs not attached to anything | ~$3.65/month each |
| `snapshots_old` | Your snapshots older than 90 days, not used by an AMI or AWS Backup | size × $0.05/GB-month (upper bound) |
| `ec2_stopped_long` | Instances stopped for more than 7 days | its attached EBS volumes |

Details, cost formulas and suggested fixes: [docs/detectors.md](docs/detectors.md).

License: MIT
