# 0001: Stop, never delete

- **Status:** accepted
- **Date:** 2026-11-19 (a project principle since the start; recorded here)

## Context

CloudTripwire acts inside the user's own AWS account. The resources it finds
(unattached volumes, old snapshots, expired instances, instances launched with a
leaked key) usually are waste, but not always: a "zombie" volume can hold the only
copy of someone's data, and a stopped instance can be a lab they come back to.
Deleting is permanent. A tool that deletes on a wrong guess loses the user's trust
and their data. Reddit feedback on a similar scanner was that people won't run
something with access to their account at all; one that can delete is a harder sell.

## Decision

In v1 CloudTripwire never deletes, terminates or releases anything. The only write
actions it takes are reversible:

- stop an EC2 or RDS instance (TTL, kill switch),
- set an access key to Inactive (kill switch),
- add its own tags (`tripwire:stopped-at`, `tripwire:quarantined`).

Deletion stays the user's decision. The sweep alert and logs show a suggested
command (`aws ec2 delete-volume ...`) that the user runs themselves.

## Why

- **Every action can be undone** with one command, and that command is in the alert
  and the DynamoDB action log.
- **Stopping already stops most of the bill.** A stopped instance stops compute
  charges; the user can delete its storage after checking it.
- **Enforced twice:** the code has no delete calls, and CI (`scripts/check_iam.py`)
  fails if any IAM policy grants `Delete*`, `Terminate*`, `Release*` or a wildcard
  action. So even a bug can't delete.

## Consequences

- Some waste keeps costing money until the user acts: EBS volumes and snapshots,
  Elastic IPs, NAT gateways and load balancers can't be "stopped", so for them
  CloudTripwire only reports.
- AWS restarts stopped RDS instances after 7 days; the TTL Lambda stops them again
  on its next run (docs/ttl.md).
- Deleting could come later as an explicit opt-in, but it would need a new ADR.
