# Kill switch

The kill switch reacts to leaked or misused credentials. This page covers the trigger
events and what the parser (`src/tripwire/killswitch/events.py`) takes from each.
Actions are in CLAUDE.md section 6.3.

## What the handler does (`src/tripwire/killswitch/handler.py`)

Each step runs even if an earlier one failed; every failure becomes an alert line.

1. **Key.** Health events don't name the user, so it is looked up with
   `iam:GetAccessKeyLastUsed` (GuardDuty `IAMUser` findings name it). Then
   `iam:UpdateAccessKey Status=Inactive`; never deleted. Temporary credentials (`ASIA...`,
   or any non-`IAMUser` type) can't be deactivated: the alert says how to revoke the
   role's sessions instead. CryptoCurrency findings have no key, so this step is skipped.
2. **Instances.** In every enabled region, `pending`/`running` instances launched within
   `killswitch_lookback_minutes` are stopped and tagged `tripwire:quarantined=true`.
   `tripwire=ignore` instances are skipped silently; Auto Scaling group members and Spot
   Instances are alert only (same rule as TTL, `common/tags.ec2_blocked_reason`).
3. **Log.** Each action goes to the DynamoDB action log with its undo command. The key
   undo there has the full key ID so it can be run as is; CloudWatch logs and alerts
   only ever show the last 4 characters. IAM error messages contain the key ID, so only
   error codes are logged.
4. **Alert.** One message, always (even when nothing was stopped): trigger, account,
   failures first, then what was done or would be done, then the manual next steps
   (rotate the key, CloudTrail, unknown IAM users/keys/instances, AWS Support).

`killswitch_mode = observe` makes no IAM/EC2 write calls and writes no action log; the
alert says "Would deactivate" / "Would stop".

**GuardDuty sample findings are alert only, in every mode.** Findings with
`detail.service.additionalInfo.sample = true` (from `aws guardduty
create-sample-findings` or the console's "Generate sample findings") send one
"sample finding received, no action taken" alert and log `{"event": "sample_finding"}`.
The check runs before any AWS call: no key lookup, no `UpdateAccessKey`, no
`StopInstances`, no action log. Sample findings name fake resources, but a recently
launched instance of yours would otherwise be stopped by the lookback rule. This makes
`create-sample-findings` a safe way to test the event routing. Fixture:
`tests/fixtures/guardduty_sample.json`.

Not handled in v1: the instance named in a CryptoCurrency finding is only stopped if it
falls inside the lookback window; repeat GuardDuty findings (default every 6 hours)
re-run the steps and re-alert (already-inactive keys and stopped instances are no-ops).

## Trigger events (checked 2026-11-19)

Every event is reduced to a `KillTarget`: `source`, `finding_type`, `account_id`,
`region`, `access_key_id`, `user_name`, `user_type`, `instance_id`. Unknown, unhandled
or malformed events return `None` and log `killswitch_event_skipped` with a reason
(the key ID is never logged). Sample events: `tests/fixtures/`.

### AWS Health: `AWS_RISK_CREDENTIALS_EXPOSED`

| Field | Path |
|---|---|
| match | `source = aws.health`, `detail-type = AWS Health Event`, `detail.eventTypeCode = AWS_RISK_CREDENTIALS_EXPOSED` (`detail.service = RISK`, category `issue`) |
| access key ID | `detail.affectedEntities[].entityValue` (first non-empty) |
| IAM user | **not in the event**. The handler must look it up from the key. |
| region | `region` is where the event was delivered (us-east-1 for this event); `detail.eventRegion` is the affected region |

AWS also restricts some API calls for the exposed key when it raises this event.

Sources:
- Envelope and fields: https://docs.aws.amazon.com/health/latest/ug/aws-health-events-eventbridge-schema.html
- The AWS docs don't show this event type. Its shape is from AWS's own sample:
  https://github.com/aws/aws-health-tools/tree/master/automated-actions/AWS_RISK_CREDENTIALS_EXPOSED
  (README test event; its Lambda reads `detail.affectedEntities[0].entityValue` and finds
  the user with `iam:GetAccessKeyLastUsed`).

### GuardDuty findings

**Needs GuardDuty enabled** (a paid service after its 30-day trial). GuardDuty isn't
available on the AWS **Free account plan**: `create-detector` fails with
`SubscriptionRequiredException` (seen 2026-11-20 in a Free plan account; check yours
with `aws freetier get-account-plan-state`). Without GuardDuty this trigger never
fires. The AWS Health trigger above needs nothing enabled and works on any plan.

Envelope: `source = aws.guardduty`, `detail-type = GuardDuty Finding`, `detail` is the
finding. Handled types: prefix `CryptoCurrency:EC2/` and
`UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration` (`.InsideAWS`, `.OutsideAWS`).

| Field | Path |
|---|---|
| finding type | `detail.type` |
| account / region | `detail.accountId`, `detail.region` (fall back to the envelope) |
| access key | `detail.resource.accessKeyDetails.accessKeyId`, `.userName`, `.userType` |
| instance | `detail.resource.instanceDetails.instanceId` |

- **CryptoCurrency:EC2/** findings have resource type `Instance`: an instance ID, **no
  access key**. The finding says which instance is mining, not which key launched it.
- **InstanceCredentialExfiltration** findings have resource type `AccessKey`, but the key
  is an EC2 instance role's **temporary** credential (`ASIA...`); `userName` is the role
  name. `iam:UpdateAccessKey` only works on IAM user keys (`AKIA...`), so the handler
  can't deactivate these. Revoking role sessions needs a deny policy on the role, which
  is an IAM write outside the current spec.

Sources:
- https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_findings_eventbridge.html
- https://docs.aws.amazon.com/guardduty/latest/APIReference/API_Finding.html
- https://docs.aws.amazon.com/guardduty/latest/APIReference/API_Resource.html
- https://docs.aws.amazon.com/guardduty/latest/APIReference/API_AccessKeyDetails.html
- https://docs.aws.amazon.com/guardduty/latest/APIReference/API_InstanceDetails.html
- https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_finding-types-ec2.html
- https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_finding-types-iam.html
- https://docs.aws.amazon.com/guardduty/latest/ug/compromised-creds.html (AKIA = long-term
  user/root key, ASIA = temporary STS credential)

Not confirmed by the docs: the exact `userType` values (the fixture uses `AssumedRole`)
and the `iamInstanceProfile` sub-fields in the crypto fixture.
