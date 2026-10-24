# 0002: Kill switch defaults to enforce

- **Status:** accepted
- **Date:** 2026-10-24

## Context

CloudTripwire's rule is "observe mode is the default": nothing changes in your
account until you switch to `enforce`. The sweep and TTL Lambdas follow it.

The kill switch is different. It runs when AWS reports one of your access keys
as exposed (AWS Health `AWS_RISK_CREDENTIALS_EXPOSED`) or GuardDuty reports
crypto mining or credential exfiltration. Bots scan public GitHub pushes and
use leaked keys within about 5–10 minutes, usually to start expensive
instances in many regions. An alert that waits for a person to read it, log in
and act often arrives after the damage has started.

## Decision

`killswitch_mode` defaults to `enforce`. On a trigger the kill switch:

1. sets the leaked access key to **Inactive** (not deleted),
2. stops instances launched in the last `killswitch_lookback_minutes`
   (not terminated), tagging them `tripwire:quarantined=true`,
3. alerts with what it did and how to undo it.

`mode` (sweep + TTL) still defaults to `observe`.

## Why

- **Speed:** the attack runs in minutes; an automatic response is the only
  one fast enough.
- **Every action is reversible:** `aws iam update-access-key --status Active`
  and `aws ec2 start-instances` undo it. Nothing is deleted, terminated or
  released, so "stop, never delete" still holds.
- **Narrow trigger:** it only fires on AWS's own exposed-credential event or
  specific GuardDuty findings, not on heuristics of ours.
- `tripwire=ignore` is still respected for instances.

## Consequences

- A false positive (or an instance you launched in the same window) can
  briefly stop something you wanted running. The alert lists exactly what was
  stopped with the command to restart it.
- Users who want to review first set `killswitch_mode = "observe"`.
- The README safety section must state this exception plainly.
