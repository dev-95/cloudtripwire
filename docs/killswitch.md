# Kill switch

The kill switch reacts to leaked or misused credentials. This page covers the trigger
events and what the parser (`src/tripwire/killswitch/events.py`) takes from each.
Actions are in CLAUDE.md section 6.3; this page grows as the handler lands.

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
