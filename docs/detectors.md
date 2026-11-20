# Detectors

Each detector runs once per enabled region during the daily sweep. Detectors are
read-only: they never stop, delete, or release anything. Any resource tagged
`tripwire=ignore` (key and value case-insensitive) is skipped.

Costs are estimates from the us-east-1 list prices in
[`src/tripwire/common/pricing.py`](../src/tripwire/common/pricing.py) (verified
2026-10-19). Prices in other regions can differ.

Fix commands are suggestions for **you** to run. CloudTripwire never runs them. They
are written to the sweep Lambda's CloudWatch logs (one `finding` event per resource,
with `fix_command`); the digest itself stays short.

**Age in the digest** ("flagged for N days") is counted from when CloudTripwire
first flagged the resource (`first_seen` in the state table), the same for every
detector. If a resource stops being flagged (e.g. a volume gets attached) and is
flagged again later, the count starts over. Each section below also notes the AWS
timestamp the detector reads; that is only used internally.

## `ebs_unattached`

**Rule:** EBS volume in state `available` (not attached to any instance).

**Cost:** `size_GB × per-GB-month price for the volume type`. Storage only;
provisioned IOPS and throughput on io1/io2/gp3 are not included. Unknown volume
types are priced as gp2 and logged.

| Type | $/GB-month |
|---|---|
| gp3 | 0.08 |
| gp2 | 0.10 |
| io1 / io2 | 0.125 |
| st1 | 0.045 |
| sc1 | 0.015 |
| standard | 0.05 |

**Timestamp read:** `CreateTime`.

**Suggested fix:** snapshot, then delete.

```
aws ec2 create-snapshot --volume-id vol-0a1b... --description "Backup of vol-0a1b..." --region us-east-1 && aws ec2 delete-volume --volume-id vol-0a1b... --region us-east-1
```

Chaining is safe: `DeleteVolume` waits for a pending snapshot of the volume to
complete ([AWS re:Post](https://repost.aws/knowledge-center/ebs-resolve-volume-stuck-deleting-state)).
`&&` needs bash, cmd, or PowerShell 7+; in Windows PowerShell 5.1 run the two
commands one after the other.

## `eip_unassociated`

**Rule:** Elastic IP with no `AssociationId` (not attached to an instance or
network interface).

**Cost:** `$0.005/hour × 730 hours = $3.65/month` (public IPv4 price, idle or in use).

**Timestamp read:** none; Elastic IPs have no creation time in the API.

**Suggested fix:**

```
aws ec2 release-address --allocation-id eipalloc-0abc... --region us-east-1
```

Releasing gives the address back to AWS; you usually can't get the same IP again.

## `nat_idle`

**Rule:** NAT gateway in state `available` that sent less than **1 MB**
(1,000,000 bytes) in the last `nat_idle_days` (default 7), measured as the sum of
CloudWatch `AWS/NATGateway` `BytesOutToDestination` (dimension `NatGatewayId`,
statistic `Sum`, period 1 day). 1 MB allows for stray health checks and NTP; any
real workload sends far more.

- **No datapoints count as zero bytes.** CloudWatch publishes nothing for a
  period with no traffic, so a NAT nobody uses has no datapoints at all.
- **NAT gateways younger than `nat_idle_days` are skipped** (by `CreateTime`).
  They don't have a full window of data yet, and because of the rule above a
  brand-new NAT would otherwise look idle.

**Cost:** `$0.045/hour × 730 hours = $32.85/month`. Data processing
($0.045/GB) is extra but near zero for an idle NAT.

**Timestamp read:** `CreateTime` (also used to skip young NAT gateways, above).

**Suggested fix:**

```
aws ec2 delete-nat-gateway --nat-gateway-id nat-0abc... --region us-east-1
```

The NAT's Elastic IP is not released when the NAT is deleted; it then shows up as
`eip_unassociated`. Private subnets routing through the NAT lose outbound internet
access.

## `elb_no_targets`

**Rule:** Application or Network Load Balancer where no target group is attached,
or every attached target group has zero registered targets. A target counts as
registered in any health state (`healthy`, `unhealthy`, `unused`, `draining`,
...). Gateway Load Balancers are skipped. Tags are read with `DescribeTags` in
batches of 20 ARNs (the API limit).

**Cost:** `hourly LB price × 730`: ALB and NLB $0.0225/hour = $16.43/month.
LCU/NLCU charges are extra but near zero with no traffic.

**Timestamp read:** `CreatedTime`.

**Suggested fix:**

```
aws elbv2 delete-load-balancer --load-balancer-arn arn:aws:elasticloadbalancing:... --region us-east-1
```

Target groups are not deleted with the load balancer and cost nothing on their own.

**Known limitation:** an ALB that only serves redirects or fixed responses (for
example, HTTP → HTTPS redirect on a domain with no backend) has no targets on
purpose and will be flagged. Tag it `tripwire=ignore`.

## `snapshots_old`

**Rule:** EBS snapshot owned by this account (`OwnerIds=self`) whose `StartTime`
is more than `snapshot_max_age_days` (default 90) ago. A snapshot exactly at the
threshold is not flagged. Skipped:

- Snapshots referenced by any of your AMIs (`describe_images Owners=self`,
  `BlockDeviceMappings[].Ebs.SnapshotId`). AWS refuses to delete those anyway,
  and they are what the AMI is made of.
- Snapshots managed by **AWS Backup** (any tag key starting with `aws:backup:`).
  Deleting one outside AWS Backup breaks that recovery point; set retention in
  your backup plan instead.

**Cost:** `VolumeSize GB × $0.05/GB-month` (standard tier). This is an upper-bound
**estimate**: snapshots are incremental, so the billed size is often much smaller
than the source volume size.

**Timestamp read:** `StartTime` (the age rule above).

**Suggested fix:**

```
aws ec2 delete-snapshot --snapshot-id snap-0a1b... --region us-east-1
```

## `ec2_stopped_long`

**Rule:** instance in state `stopped` for more than `stopped_instance_days`
(default 7). A stopped instance has no compute cost but still pays for its EBS
volumes. A stop exactly at the threshold is not flagged.

**Stop time:** the EC2 API has no stop timestamp field, so it is read from
`StateTransitionReason`, e.g. `User initiated (2026-10-01 12:00:00 GMT)`. If the
reason has no timestamp in that format, the instance is **skipped** and a
`unparseable_stop_reason` warning is logged. This is deliberate: without a stop
time we can't tell how long it has been stopped, and a false alarm is worse than
a missed one for this detector.

**Cost:** sum of the [`ebs_unattached`](#ebs_unattached) per-GB-month prices for
every EBS volume attached to the instance (one `DescribeVolumes` call per instance).

**Timestamp read:** the stop time from `StateTransitionReason` (the rule above).

**Suggested fix:** make an AMI as a backup, wait for it, then terminate.

```
aws ec2 create-image --instance-id i-0a1b... --name "backup-i-0a1b..." --region us-east-1 && aws ec2 wait image-available --owners self --filters Name=name,Values=backup-i-0a1b... --region us-east-1 && aws ec2 terminate-instances --instance-ids i-0a1b... --region us-east-1
```

Notes: the AMI's snapshots are billed at snapshot prices, so deregister the AMI
when you no longer need it. Volumes with `DeleteOnTermination=false` survive
termination and keep billing (CloudTripwire will then flag them as
`ebs_unattached`). `&&` needs bash, cmd, or PowerShell 7+; in Windows PowerShell
5.1 run the three commands one after the other.
