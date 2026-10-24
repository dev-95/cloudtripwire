# TTL timers

Tag a resource with `ttl` and CloudTripwire stops it when the time is up. The
TTL Lambda runs hourly (`ttl_schedule`). Code:
[`src/tripwire/ttl/handler.py`](../src/tripwire/ttl/handler.py).

## What is checked

| Resource | Checked when | Clock starts at |
|---|---|---|
| EC2 instance | state `running` | `LaunchTime` (resets every time the instance starts) |
| RDS DB instance | status `available` | `InstanceCreateTime` (fixed) |

## Tag format

Key `ttl` (any case). Values:

- Relative to the clock start: `30m`, `4h`, `2d`, `1w`
- Absolute, UTC: `2026-10-20` (midnight) or `2026-10-20T18:00Z`

Anything else is **unreadable**: you get one alert naming the value, and nothing
is done.

## What happens

| Situation | `observe` (default) | `enforce` |
|---|---|---|
| Less than 1 hour left | One "expires in N min" alert | Same |
| Expired | One "would stop X (ttl 4h, expired 20 min ago)" alert | Stop it, tag `tripwire:stopped-at=<UTC time>`, log the action in DynamoDB, alert with the undo command |
| Expired, but it can't safely be stopped (below) | One "won't stop" alert with the reason | Same: never stopped |
| Tagged `tripwire=ignore` | Nothing, ever | Nothing, ever |

All alerts from one run go out as one message. Each alert is sent once per
resource and expiry: if you change the `ttl` tag, the new expiry gets its own
warning and expiry alert.

Errors are handled per region and per resource: one failure is logged
(`check_failed`) and the run continues. As with the sweep, a run with any
failed check always sends a message that includes "Warning: N checks failed".

## Never stopped (alert only)

| Case | Why |
|---|---|
| EC2 instance with the `aws:autoscaling:groupName` tag | The Auto Scaling group would launch a replacement. Scale the group to 0 instead. |
| EC2 Spot Instance | Manage it through its Spot request. |
| Aurora cluster member (`DBClusterIdentifier` set) | Aurora instances are stopped per cluster (`aws rds stop-db-cluster`). |
| RDS read replica, or an instance that has read replicas | RDS can't stop either. |
| Multi-AZ RDS for SQL Server | RDS can't stop it. |

Source for the RDS limits: RDS User Guide, "Stopping an Amazon RDS DB instance
temporarily" (checked 2026-10-24).

## Lab mode

With `lab_mode = true` (off by default), a running EC2 or available RDS instance
with **no** `ttl` tag is treated as if its ttl were `lab_grace_hours` (default
24) from its clock start. Meant for dedicated learning accounts. `tripwire=ignore`
still opts a resource out.

## Restarting something CloudTripwire stopped

The alert includes the command, e.g.
`aws ec2 start-instances --instance-ids i-0abc --region us-east-1`.

- **EC2:** relative ttls count from `LaunchTime`, which resets when the instance
  starts, so a restarted instance gets a fresh window. An absolute date that has
  passed stops it again on the next run: change or remove the tag first.
- **RDS:** `InstanceCreateTime` never changes, so an expired ttl stays expired.
  Change or remove the tag before starting it, or the next run stops it again.

## RDS restarts itself after 7 days

AWS automatically starts a DB instance that has been stopped for 7 consecutive
days. If its ttl is still expired, the next hourly run stops it again (and alerts
again, in `enforce` mode). So a ttl-expired RDS instance runs for at most about an
hour every 7 days. If you want it gone for good, snapshot and delete it yourself;
CloudTripwire never deletes.

## Permissions

See [permissions.md](permissions.md#ttl-lambda-cloudtripwire-ttl).
