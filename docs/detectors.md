# Detectors

Each detector runs once per enabled region during the daily sweep. Detectors are
read-only: they never stop, delete, or release anything. Any resource tagged
`tripwire=ignore` (key and value case-insensitive) is skipped.

Costs are estimates from the us-east-1 list prices in
[`src/tripwire/common/pricing.py`](../src/tripwire/common/pricing.py) (verified
2026-10-19). Prices in other regions can differ.

Fix commands are suggestions for **you** to run. CloudTripwire never runs them.

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

**Age:** days since the volume's `CreateTime` (the sweep handler will replace this
with days since CloudTripwire first saw it).

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

**Age:** not reported; Elastic IPs have no creation time in the API.

**Suggested fix:**

```
aws ec2 release-address --allocation-id eipalloc-0abc... --region us-east-1
```

Releasing gives the address back to AWS; you usually can't get the same IP again.
