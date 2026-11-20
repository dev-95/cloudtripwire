# Architecture

Everything runs in your own AWS account, in the region you deploy to (the **home
region**), plus two forwarding rules in us-east-1. Nothing leaves the account except
the alerts you configure (Telegram, email). No SaaS, no telemetry.

```
                         home region                                   every enabled region
 EventBridge daily ──► cloudtripwire-sweep ──── read only ──────────► EC2, ELBv2, CloudWatch
 EventBridge hourly ─► cloudtripwire-ttl ────── stop (enforce) ─────► EC2, RDS
 Health / GuardDuty ─► cloudtripwire-killswitch ─ deactivate key ───► IAM
                                     │          └ stop new instances ► EC2
                                     ▼
              DynamoDB cloudtripwire-state (findings, action log, alert markers)
              SSM /cloudtripwire/telegram_* ──► Telegram     SNS cloudtripwire-alerts ──► email
```

| Lambda | Trigger | Reads | Writes |
|---|---|---|---|
| `cloudtripwire-sweep` | daily schedule (`sweep_schedule`) | EC2, ELBv2, CloudWatch in every enabled region | state table (findings), alerts. Never changes your resources. |
| `cloudtripwire-ttl` | hourly schedule (`ttl_schedule`) | EC2, RDS in every enabled region | stops expired EC2/RDS (enforce), action log, alerts |
| `cloudtripwire-killswitch` | Health / GuardDuty events | EC2 in every enabled region, IAM | deactivates the key, stops new instances (enforce), action log, alerts |

Rules per Lambda: [detectors.md](detectors.md), [ttl.md](ttl.md),
[killswitch.md](killswitch.md). Every IAM permission: [permissions.md](permissions.md).

## What Terraform creates

- Three Lambdas (Python 3.12, 256 MB, 5-minute timeout) from one zip of `src/`, each
  with its own IAM role and a log group (30-day retention).
- EventBridge: two schedules (sweep, TTL), two kill switch rules in the home region,
  and, if the home region isn't us-east-1, two forwarding rules there plus the role
  they use.
- DynamoDB table `cloudtripwire-state` (on-demand, TTL attribute `expires_at`).
- SNS topic `cloudtripwire-alerts` (plus your email subscription, if set).
- SSM SecureString parameters `/cloudtripwire/telegram_bot_token` and
  `/cloudtripwire/telegram_chat_id` (placeholders; you set the values).
- CloudWatch alarms on each Lambda's `Errors` metric, sent to the SNS topic.

## Code layout

Each handler is thin: parse the event, call pure functions, write state, notify.

| Path | What |
|---|---|
| `src/tripwire/common/` | config (env vars), regions, pricing, tags/ttl parsing, DynamoDB state, notify |
| `src/tripwire/sweep/` | handler + one module per detector |
| `src/tripwire/ttl/handler.py` | TTL rules and stops |
| `src/tripwire/killswitch/` | `events.py` (event parser), `handler.py` (actions, alert) |

Only the Python standard library and boto3 (in the Lambda runtime) are used: no
layers, no `requests`. Clients are cached per region for the warm container.

## Failure handling

- **Per region / per check:** a failing region, service or resource is logged and
  skipped; the rest of the run continues and the alert says "Warning: N checks failed".
- **Per kill switch step:** deactivate, stop and log are independent; a failure is an
  alert line, not a crash.
- **Per channel:** a failing alert channel is logged and doesn't block the others.
- **Crashes** (bad config, DynamoDB unreachable) make the Lambda error, which trips its
  CloudWatch alarm. The alarm sends **email only** (SNS), so set `alert_email` even if
  you use Telegram. Terraform warns if it's empty.

## Alerts

- Telegram via the Bot API (`urllib.request`, 15 s timeout), email via SNS.
- One message per run, kept under 4,000 characters (Telegram's limit is 4,096); long
  lists end with "+N more", the footer is never cut.
- Never in a message or log: full access key IDs (last 4 only), tokens, secrets. The
  account ID may appear.
- The sweep sends a digest when it finds something or a check failed, and an all-clear
  on Mondays (UTC); otherwise nothing. TTL alerts are sent once per resource and expiry.
  The kill switch always alerts.

## Kill switch event routing

```
us-east-1                                       home region (e.g. ca-central-1)
AWS Health AWS_RISK_CREDENTIALS_EXPOSED ─┐
GuardDuty finding (us-east-1)  ──────────┤ forward rules ──► default bus ──► killswitch rules ──► Lambda
                                         │ (role: events:PutEvents)            ▲
                                                 GuardDuty finding (home) ─────┘
```

- AWS Health sends global events, including exposed access keys, to **us-east-1
  only** (Health User Guide, "Choosing a Region": "To receive global events, you must
  create a rule for the US East (N. Virginia) Region").
- The module takes an `aws.us_east_1` provider and creates two rules there (Health,
  GuardDuty) that target the home region's default event bus
  (EventBridge User Guide, cross-Region event routing). The rules on the home bus use
  the same patterns and invoke the Lambda.
- If the home region is us-east-1, the forwarding rules and role aren't created;
  the home rules match the events directly.
- The Lambda, state table, SNS topic and SSM parameters all live in the home region,
  so the kill switch needs no cross-region calls except stopping instances.
- **Verified 2026-11-20** in the test account (home ca-central-1): a temporary
  us-east-1 rule using the forwarder role sent a `put-events` event (source
  `cloudtripwire.test`) to the ca-central-1 default bus. A rule there matching that
  source invoked the Lambda within 15 s, and the Lambda logged the original
  `source`. So a default bus in another region works as a target, and the event's
  `source` survives forwarding. Not tested end to end: a real `aws.health` /
  `aws.guardduty` event (`aws.*` sources can't be sent by hand, and GuardDuty isn't
  available on the account's Free plan).
- Fallback if forwarding ever stops working: a second copy of the Lambda in us-east-1
  that sends its DynamoDB/SNS/SSM calls to the home region.

## State table

One DynamoDB table (`cloudtripwire-state`, on-demand), string keys `pk` / `sk`.
Helpers live in [`src/tripwire/common/state.py`](../src/tripwire/common/state.py).

| Item | `pk` | `sk` | Attributes |
|---|---|---|---|
| Finding | `FINDING#<region>#<resource_id>` | `<detector>` | `first_seen`, `last_seen`, `status` (`open`/`resolved`), `resolved_at`, `monthly_cost_usd`, `region`, `resource_id` |
| Action log | `ACTION#<yyyy-mm-dd>` | `<iso timestamp>#<resource_id>` | the `Action` fields, `expires_at` |
| Alert-once marker | `ALERT#<resource_id>` | `<alert_type>` | `alerted_at` |

- The TTL Lambda's markers use `ALERT#<region>#<resource_id>` (RDS identifiers are
  only unique per region) and alert types `ttl_unreadable#<value>`,
  `ttl_warning#<expiry>`, `ttl_expired#<expiry>`. Including the value/expiry means a
  changed `ttl` tag alerts again; an unchanged one never repeats.
- Failed-check warnings use `ALERT#<region>` / `ttl_check_failed#<error code>#<date>`
  (once per UTC day) with `expires_at` 2 days out. Other markers have no
  `expires_at`, so they stay until deleted (a few small items per tagged resource).

- Timestamps are UTC ISO 8601 strings; costs are stored as `Decimal`.
- `expires_at` (epoch seconds, 90 days after the action) is the table's **TTL
  attribute**; DynamoDB deletes old action items for free.
- A finding that is resolved and later flagged again starts a new `first_seen`.
- A finding is only resolved when its region scanned successfully. A region that
  errored keeps its findings open.
- Open findings are read with a filtered `Scan`. At the expected scale (tens of
  findings per account, a few action items a day) that is one small page and stays
  in the free tier. Add a GSI on `status` if that ever changes.
