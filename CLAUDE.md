# CloudTripwire — Project Spec & Working Guide

> This file is the single source of truth for the project. Claude Code: read this whole file before doing anything, follow the **Working rules** section strictly, and update the **Status** section at the end of every working session.

---

## 1. What we are building

**CloudTripwire** is an open-source safety net that a person installs into **their own AWS account**. It catches the two mistakes that most often turn into large, unexpected AWS bills:

1. **Forgotten resources ("zombies")** still billing after someone thought they cleaned up (unattached EBS volumes, idle Elastic IPs, NAT gateways, empty load balancers, old snapshots, stopped instances still paying for storage).
2. **Leaked access keys** pushed to GitHub, which bots find within minutes and use to launch crypto-mining instances.

It **finds** the problem, **estimates what it costs**, **alerts** the user (Telegram / Slack / email), and, when the user allows it, **acts** (stops resources, deactivates leaked keys). It never deletes anything by default.

### One-line pitch
"Install in 2 minutes, stop surprise AWS bills: zombie cleanup, self-destruct timers, and a leaked-key kill switch, running entirely in your own account."

### Who it is for
- People learning AWS (certification students, bootcamp and college students)
- Solo developers and side projects
- Small teams with no dedicated FinOps or security person

### Why it exists (evidence)
- Reddit and AWS re:Post are full of learners getting surprise bills from orphaned EBS volumes, forgotten EC2, and NAT gateways.
- Leaked keys are exploited within ~5–10 minutes of a public push.
- Existing scanners mostly **report only** and ask users to trust third-party code or a SaaS with account access. Reddit feedback on one such tool: users won't run something with access to their account, and "pro" reporting is something anyone can cron themselves.

### Our differentiators
1. **Runs inside the user's account.** No SaaS, no data leaves the account, the author never sees credentials.
2. **Acts, not just reports**: TTL enforcement and a key kill switch.
3. **Safe by default**: observe mode first, stop-never-delete, opt-out tag, every IAM permission documented.
4. **One install** covers cost cleanup + TTL timers + leak response.

---

## 2. Owner and goals

- **Owner:** Devesh Chalasani (GitHub `dev-95`). Cloud/DevOps/SRE job seeker in Canada. Certified AZ-104, Terraform Associate, AWS AI Practitioner.
- **Dev machine:** Windows laptop, VS Code, Claude Code. Use PowerShell or Git Bash/WSL commands as appropriate; prefer cross-platform Python scripts over bash where practical.
- **Time available:** part-time, around shift work. Keep tasks small and finishable in one sitting.

### Project goals (in priority order)
1. **Real users.** Ship something people actually install. Track installs, GitHub stars, and cost flagged.
2. **Portfolio proof** for Cloud/DevOps/SRE roles: Terraform module, event-driven AWS automation, IAM least privilege, security automation, FinOps, CI with tests.
3. **Clean, readable code** a hiring manager can skim in 5 minutes.

### Target resume bullet (what we're building toward)
"Built and open-sourced CloudTripwire, an in-account AWS safety net (Terraform, Lambda, EventBridge, GuardDuty) that detects idle resources, enforces TTL tags, and auto-quarantines leaked keys; used by N people, flagging $X/month in idle spend."

---

## 3. Non-negotiable principles

These override convenience. If a feature conflicts with them, the principle wins.

1. **Stop, never delete** (v1). The tool may stop EC2/RDS and deactivate keys. It never terminates, deletes, or releases anything. Deletion is the user's decision, shown as a suggested CLI command in the alert.
2. **Observe mode is the default.** In `observe` mode the tool only reports what it *would* do. The user switches to `enforce` explicitly.
3. **Opt-out tag always respected.** Any resource tagged `tripwire=ignore` is never acted on and never flagged.
4. **Least privilege.** Each Lambda gets its own IAM role with only the actions it needs. Every permission is listed with its reason in `docs/permissions.md`.
5. **Zero runtime dependencies.** Lambdas use only the Python standard library + boto3 (bundled in the Lambda runtime). HTTP alerts use `urllib.request`. No `requests`, no layers.
6. **No secrets in the repo, ever.** Bot tokens and webhooks live in SSM Parameter Store (SecureString). The repo runs gitleaks in pre-commit and CI.
7. **Near-zero running cost** for the user: stays within Lambda / EventBridge / DynamoDB free tiers at normal scale. Document any service with a real cost (GuardDuty).
8. **Every action is logged and reversible**: written to DynamoDB, included in the alert, with the command to undo it.

---

## 4. Architecture

```
                     ┌──────────────────────── User's AWS account ────────────────────────┐
                     │                                                                     │
 EventBridge         │   ┌───────────────┐   reads all regions    ┌───────────────────┐    │
 schedule (daily) ───┼──►│ sweep Lambda  │───────────────────────►│ EC2 / ELBv2 / RDS │    │
                     │   └──────┬────────┘                        │ CloudWatch metrics│    │
                     │          │ first-seen + findings           └───────────────────┘    │
 EventBridge         │          ▼                                                          │
 schedule (hourly) ──┼──►┌───────────────┐   stop (enforce mode)                           │
                     │   │  ttl Lambda   │──────────────────────► EC2 / RDS                │
                     │   └──────┬────────┘                                                 │
                     │          ▼                                                          │
                     │   ┌───────────────┐                                                 │
                     │   │  DynamoDB     │  state: first_seen, findings, actions log       │
                     │   └───────────────┘                                                 │
                     │                                                                     │
 aws.health event    │   ┌──────────────────┐  deactivate key, stop recent instances      │
 (exposed key) ──────┼──►│ killswitch Lambda│──────────────────────► IAM / EC2            │
 GuardDuty finding ──┼──►└──────┬───────────┘                                             │
                     │          │                                                          │
                     │          ▼                                                          │
                     │   notify module ──► Telegram / Slack webhook / SNS email            │
                     │   (secrets from SSM Parameter Store)                                │
                     └─────────────────────────────────────────────────────────────────────┘

 Developer side (separate, in the user's repos):  pre-commit hook + GitHub Action running gitleaks
```

### Components

| Component | Trigger | Job |
|---|---|---|
| `sweep` Lambda | EventBridge schedule, daily (default 13:00 UTC) | Scan all enabled regions for zombies, estimate cost, record first-seen, send digest |
| `ttl` Lambda | EventBridge schedule, hourly | Enforce `ttl` tags and lab-mode rules; stop expired resources in `enforce` mode |
| `killswitch` Lambda | EventBridge rules on AWS Health exposed-credential events and selected GuardDuty findings | Deactivate the leaked key, stop instances launched recently, alert immediately |
| `notify` (shared module) | Called by all Lambdas | Format and send messages to configured channels |
| DynamoDB table `cloudtripwire-state` | — | First-seen timestamps, finding history, action log (on-demand billing) |
| SSM parameters `/cloudtripwire/*` | — | Telegram bot token + chat id, Slack webhook URL |
| SNS topic `cloudtripwire-alerts` | — | Email delivery |
| Dev guardrails | git commit / push | Block secrets before they reach GitHub |

---

## 5. Repository layout

```
cloudtripwire/
├── CLAUDE.md                     # this file
├── README.md                     # user-facing: pitch, demo GIF, install, config, safety, FAQ
├── LICENSE                       # MIT
├── .pre-commit-config.yaml       # gitleaks + ruff
├── .gitleaks.toml
├── .gitignore                    # *.tfstate*, .terraform/, *.zip, .env, __pycache__/
├── pyproject.toml                # ruff + pytest config, dev deps only
├── requirements-dev.txt          # pytest, moto[all], ruff, boto3 (local only)
├── docs/
│   ├── architecture.md
│   ├── permissions.md            # every IAM action + why
│   ├── detectors.md              # each detector: rule, cost formula, suggested fix
│   └── decisions/                # short ADRs: 0001-stop-not-delete.md, etc.
├── src/tripwire/
│   ├── common/
│   │   ├── config.py             # read env vars, validate, defaults
│   │   ├── regions.py            # list enabled regions
│   │   ├── pricing.py            # fallback price table + monthly cost helpers
│   │   ├── state.py              # DynamoDB read/write helpers
│   │   ├── notify.py             # telegram / slack / sns senders + formatting
│   │   ├── tags.py               # tag parsing, ignore check, ttl parsing
│   │   └── models.py             # dataclasses: Finding, Action
│   ├── sweep/
│   │   ├── handler.py
│   │   └── detectors/
│   │       ├── ebs_unattached.py
│   │       ├── eip_unassociated.py
│   │       ├── nat_idle.py
│   │       ├── elb_no_targets.py
│   │       ├── snapshots_old.py
│   │       └── ec2_stopped_long.py
│   ├── ttl/
│   │   └── handler.py
│   └── killswitch/
│       └── handler.py
├── terraform/                    # the reusable module (root of module)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── iam.tf
│   ├── lambda.tf
│   ├── eventbridge.tf
│   ├── dynamodb.tf
│   ├── sns.tf
│   ├── versions.tf
│   └── examples/basic/main.tf
├── cloudformation/
│   └── cloudtripwire.yaml        # one-click quick-create template (mirrors the TF module)
├── guardrails/
│   ├── pre-commit-snippet.yaml   # for users to copy into their own repos
│   └── secret-scan.yml           # reusable GitHub Action workflow for users
├── scripts/
│   ├── seed_zombies.py           # create cheap test zombies in the TEST account only
│   └── cleanup_seed.py           # remove what seed_zombies created
├── tests/
│   ├── conftest.py               # moto fixtures, fake env vars
│   ├── unit/                     # one test file per detector / module
│   └── fixtures/                 # sample health + guardduty event JSON
└── .github/workflows/
    ├── ci.yml                    # ruff, pytest, terraform fmt/validate, gitleaks
    └── release.yml               # on tag: zip lambdas, attach to GitHub release
```

---

## 6. Feature specifications

### 6.1 Zombie sweep (`sweep` Lambda)

Runs daily. For each enabled region (`ec2.describe_regions` with opt-in status `opt-in-not-required` or `opted-in`, optionally limited by the `regions` config), run every detector. Each detector returns a list of `Finding` objects:

```python
@dataclass
class Finding:
    detector: str          # "ebs_unattached"
    resource_id: str       # "vol-0a1b..."
    region: str
    monthly_cost_usd: float
    age_days: int | None   # days since first seen by CloudTripwire (or creation time)
    reason: str            # human sentence
    fix_command: str       # suggested AWS CLI command the USER can run (never run by us)
```

Skip any resource tagged `tripwire=ignore`.

| Detector | Rule | Cost basis | Suggested fix |
|---|---|---|---|
| `ebs_unattached` | Volume state `available` | size_GB × per-GB-month price by volume type | `aws ec2 delete-volume` (after snapshot if needed) |
| `eip_unassociated` | Elastic IP with no `AssociationId` | hourly public IPv4 price × 730 | `aws ec2 release-address` |
| `nat_idle` | NAT gateway `available` with ~0 bytes (`BytesOutToDestination` sum < 1 MB) over the last 7 days | hourly NAT price × 730 (+ note data processing) | `aws ec2 delete-nat-gateway` |
| `elb_no_targets` | ALB/NLB where all target groups have zero registered targets | hourly LB price × 730 | `aws elbv2 delete-load-balancer` |
| `snapshots_old` | Snapshot owned by the account, older than 90 days (configurable), not referenced by any AMI | size_GB × snapshot price (estimate, real billed size can be smaller) | `aws ec2 delete-snapshot` |
| `ec2_stopped_long` | Instance `stopped` for more than 7 days (configurable) | sum of attached EBS volume costs | Snapshot + terminate, or delete volumes |

**Age tracking:** When a resource is first flagged, write `first_seen` to DynamoDB. Report "unattached for N days" from that. When a resource is no longer flagged, mark it resolved.

**Pricing:** v1 uses a **fallback price table** in `pricing.py` (us-east-1 list prices, clearly labelled "estimate"). Values must be verified against the AWS pricing pages before release. The approximate starting points to verify:
- gp3 / gp2 EBS ~ $0.08 / $0.10 per GB-month
- public IPv4 address ~ $0.005 per hour (~$3.65/month)
- NAT gateway ~ $0.045 per hour (~$32.85/month) + data processing
- ALB ~ $0.0225 per hour (+ LCU)
- EBS snapshot ~ $0.05 per GB-month

v2 can query the AWS Price List API (`pricing` client, `us-east-1` endpoint) per region.

**Digest message** (one per run, only if findings exist, or a weekly "all clear"):

```
🧟 CloudTripwire: 3 zombies found, about $41/month
• NAT gateway nat-0abc (us-east-1): $32.85/mo, no traffic in 7 days
• EBS vol-0a1b, 20 GB gp3 (us-east-1): $1.60/mo, unattached 12 days
• Elastic IP 3.84.x.x (ca-central-1): $3.65/mo, not attached
Fix commands: in the sweep Lambda's CloudWatch logs.
Mode: observe (nothing was changed)
```

### 6.2 TTL enforcement (`ttl` Lambda)

Runs hourly. Supported resource types in v1: **EC2 instances, RDS DB instances**. Other resource types carrying a `ttl` tag are alert-only.

**Tag format** (key `ttl`, case-insensitive):
- Duration relative to creation time: `30m`, `4h`, `2d`, `1w`
- Absolute: `2026-10-20` or `2026-10-20T18:00Z` (UTC)
- Invalid values → alert once per resource ("ttl tag unreadable"), take no action.

**Creation time:** EC2 `LaunchTime`, RDS `InstanceCreateTime`.

**Behaviour when expired:**
- `observe` mode: alert "would stop X (ttl 4h expired 20 min ago)".
- `enforce` mode: stop the resource, tag it `tripwire:stopped-at=<ISO time>`, log to DynamoDB, alert with the undo command (`aws ec2 start-instances ...`).
- One-hour warning alert before expiry (only once per resource).

**Lab mode** (`lab_mode = true`, off by default): running EC2/RDS with **no** `ttl` tag and older than `lab_grace_hours` (default 24) are treated as expired. Intended for dedicated learning accounts.

**Note:** AWS automatically restarts stopped RDS instances after 7 days. The TTL Lambda will stop them again on its next run; document this.

### 6.3 Leaked-key kill switch (`killswitch` Lambda)

**Triggers (EventBridge rules):**
1. AWS Health events for exposed credentials: `source = aws.health`, `detail-type = AWS Health Event`, `detail.eventTypeCode = AWS_RISK_CREDENTIALS_EXPOSED`. These events are delivered in **us-east-1**, so this rule and the Lambda must exist there.
2. GuardDuty findings (only if GuardDuty is enabled): types starting with `CryptoCurrency:EC2/` and `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration`. GuardDuty findings are regional; v1 covers the deployment region + us-east-1, multi-region is v2.

> Claude Code: verify the exact event field names against current AWS documentation and save sample events in `tests/fixtures/` before writing the parser.

**Actions** (always in this order, each step logged; failures in one step must not stop the others):
1. Identify the access key ID and IAM user from the event.
2. `iam.update_access_key(Status="Inactive")` for that key. (Reversible; do not delete.)
3. In every enabled region, find instances launched in the last `killswitch_lookback_minutes` (default 60) and stop them, tagging `tripwire:quarantined=true`. Skip `tripwire=ignore`.
4. Alert immediately on all channels with: key ID (masked to last 4 chars), user, what was stopped, and the next manual steps (rotate key, check CloudTrail, check for new IAM users, contact AWS Support for billing).

**Mode:** controlled separately by `killswitch_mode` (`observe` | `enforce`). **OPEN DECISION for Devesh:** default `enforce` (fast response matters more; actions are reversible) vs `observe` (consistent with the global safety default). Until decided, default to `observe` and make it a one-line change.

### 6.4 Notifications (`notify.py`)

- Channels enabled by config: `telegram`, `slack`, `email` (SNS). Any combination.
- Secrets read at runtime from SSM: `/cloudtripwire/telegram_bot_token`, `/cloudtripwire/telegram_chat_id`, `/cloudtripwire/slack_webhook_url`.
- Use `urllib.request` with a 10-second timeout. A failing channel logs an error and does not block other channels.
- Messages under 4,000 characters (Telegram limit); truncate the finding list with "+N more".
- Never include full access keys, tokens, or account secrets in messages. Account ID may be shown.

### 6.5 Developer guardrails (for users' own repos)

- `guardrails/pre-commit-snippet.yaml`: gitleaks hook users paste into their `.pre-commit-config.yaml`.
- `guardrails/secret-scan.yml`: GitHub Actions workflow running gitleaks on push and PR.
- README section: "Stop leaks before they happen" with 3-step setup.

---

## 7. Configuration

All set through Terraform variables (and mirrored as CloudFormation parameters), passed to Lambdas as environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `mode` | `observe` | `observe` or `enforce` for sweep + TTL actions |
| `killswitch_mode` | `observe` | See open decision in 6.3 |
| `regions` | `[]` (all enabled) | Limit scanning to these regions |
| `sweep_schedule` | `cron(0 13 * * ? *)` | Daily digest time (UTC) |
| `ttl_schedule` | `rate(1 hour)` | TTL check frequency |
| `lab_mode` | `false` | Treat untagged running resources as expired after grace |
| `lab_grace_hours` | `24` | Grace period for lab mode |
| `snapshot_max_age_days` | `90` | Old snapshot threshold |
| `stopped_instance_days` | `7` | Stopped-too-long threshold |
| `nat_idle_days` | `7` | NAT traffic lookback |
| `killswitch_lookback_minutes` | `60` | Instances launched within this window get stopped |
| `channels` | `["telegram"]` | Alert channels |
| `alert_email` | `""` | SNS email subscription (user must confirm) |
| `tags` | `{}` | Tags applied to all CloudTripwire resources |

Terraform outputs: Lambda ARNs, DynamoDB table name, SNS topic ARN, the SSM parameter names the user must fill in.

---

## 8. IAM (least privilege)

One role per Lambda. All three get `logs:CreateLogStream`, `logs:PutLogEvents` on their own log group and `ssm:GetParameter` on `/cloudtripwire/*`, plus only the DynamoDB item actions each Lambda's code calls, on the state table only. No `kms:Decrypt`: the AWS managed `aws/ssm` key's policy already allows decryption through SSM (verified 2026-10-24).

| Lambda | Read actions | Write actions |
|---|---|---|
| sweep | `ec2:DescribeRegions`, `ec2:DescribeVolumes`, `ec2:DescribeAddresses`, `ec2:DescribeNatGateways`, `ec2:DescribeSnapshots`, `ec2:DescribeImages`, `ec2:DescribeInstances`, `elasticloadbalancing:DescribeLoadBalancers`, `elasticloadbalancing:DescribeTargetGroups`, `elasticloadbalancing:DescribeTargetHealth`, `elasticloadbalancing:DescribeTags`, `cloudwatch:GetMetricStatistics` | `dynamodb:UpdateItem`, `dynamodb:Scan` (state table), `sns:Publish` (own topic) |
| ttl | `ec2:DescribeRegions`, `ec2:DescribeInstances`, `rds:DescribeDBInstances`, `rds:ListTagsForResource` | `ec2:StopInstances`, `ec2:CreateTags`, `rds:StopDBInstance`, `rds:AddTagsToResource`, `sns:Publish` |
| killswitch | `ec2:DescribeRegions`, `ec2:DescribeInstances`, `iam:ListAccessKeys` | `iam:UpdateAccessKey`, `ec2:StopInstances`, `ec2:CreateTags`, `sns:Publish` |

- Where possible, scope write actions with a condition that excludes resources tagged `tripwire=ignore`, in addition to the code check.
- **No** `Delete*`, `Terminate*`, `Release*`, or `iam:*` wildcards anywhere. CI should fail if a policy contains them (simple grep check in `ci.yml`).
- Keep `docs/permissions.md` in sync with `terraform/iam.tf`.

---

## 9. Testing strategy

- **Unit tests with moto** for every detector, the TTL parser, the TTL handler, the kill switch handler, and notify (HTTP mocked). No network and no real AWS in tests.
- Fixtures: sample AWS Health and GuardDuty event JSON in `tests/fixtures/`.
- Must-have test cases:
  - Each detector finds the zombie and ignores a healthy resource and a `tripwire=ignore` resource.
  - TTL parsing: every valid format, invalid values, timezone handling.
  - `observe` mode never calls stop / update APIs (assert with moto state or a spy).
  - Kill switch: key deactivated, recent instance stopped, old instance untouched, one failing step doesn't stop the others.
  - Notify: message truncation, one channel failing doesn't block others, no secrets in output.
- Target: full suite under 10 seconds.
- **Integration test (manual, test account only):** `scripts/seed_zombies.py` creates a 1 GB unattached gp3 volume, an unassociated EIP, and a t3.micro with `ttl=30m`. Deploy, run the Lambdas manually, confirm alerts, then `scripts/cleanup_seed.py`. NAT gateway testing is optional (it costs ~$1/day); delete it the same day.

---

## 10. CI/CD

`ci.yml` on every push and PR:
1. `ruff check` + `ruff format --check`
2. `pytest -q`
3. `terraform fmt -check -recursive` and `terraform validate` (in `terraform/` and `terraform/examples/basic`)
4. gitleaks scan
5. IAM wildcard/destructive-action check (grep for `Delete`, `Terminate`, `Release`, `"*"` actions in `terraform/iam.tf` and `cloudformation/`)

A failing test blocks everything after it.

`release.yml` on tag `v*`: zip each Lambda package, attach zips + `cloudformation/cloudtripwire.yaml` to the GitHub release.

CloudFormation one-click link (README): `https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?templateURL=<public S3 URL of the template>&stackName=cloudtripwire`. Hosting the template publicly requires a public S3 bucket owned by Devesh. **OPEN DECISION:** set this up at v0.2 launch, or ship Terraform-only first.

---

## 11. Coding conventions

- Python 3.12 (Lambda runtime `python3.12`). Type hints everywhere. Dataclasses for models.
- Each handler is thin: parse event → call pure functions → write state → notify. Business logic lives in pure, testable functions.
- boto3 clients created per region through a small helper; reuse within an invocation.
- Use paginators for every list/describe call.
- Structured logging: `logging` with JSON-ish messages (`{"event": "finding", "detector": ..., "resource": ...}`).
- Handle `ClientError` per region so one broken region doesn't kill the run.
- Terraform: `>= 1.5`, AWS provider pinned (`~> 5.0` or current major), `archive_file` to zip `src/`, variables with descriptions and validation blocks, no hard-coded account IDs or regions.
- Commit messages: imperative, short (`Add EIP detector with tests`).

---

## 12. Milestones

Each milestone ends with tests passing and something demoable.

### M1 — Foundation + sweep (week 1)
- [x] Repo scaffold, MIT license, `.gitignore`, pre-commit with gitleaks + ruff
- [x] `common/` modules: config, regions, tags, pricing (fallback table), models
- [x] Detectors: `ebs_unattached`, `eip_unassociated` with moto tests
- [x] Detectors: `nat_idle`, `elb_no_targets`, `snapshots_old`, `ec2_stopped_long` with tests
- [x] DynamoDB state (first_seen, resolved)
- [x] `notify` for Telegram + email, with tests
- [x] Sweep handler + digest formatting
- [x] Terraform module: DynamoDB, SNS, sweep Lambda, IAM, schedule; `examples/basic` (deployed to the test account 2026-10-23)
- [x] CI workflow green (run #3, commit `4a64a35`)
- **Demo:** seeded zombies in test account → Telegram digest arrives. **Passed 2026-10-23.**

### M2 — TTL + kill switch (week 2)
- [ ] TTL parser + tests
- [ ] TTL handler (observe + enforce), warnings, lab mode
- [ ] Kill switch handler + fixtures + tests
- [ ] Terraform: ttl + killswitch Lambdas, IAM, EventBridge rules (us-east-1 for Health events)
- [ ] Slack channel
- [ ] Guardrails: pre-commit snippet + GitHub Action for users
- [ ] IAM destructive-action check in CI
- **Demo:** t3.micro with `ttl=30m` gets stopped in enforce mode; simulated Health event deactivates a test key.

### M3 — Launch (week 3)
- [ ] CloudFormation template mirroring the module (if decided)
- [ ] `docs/` complete: architecture, permissions, detectors, ADRs
- [ ] README: pitch, demo GIF, 2-minute install, config table, safety section, cost section, FAQ, uninstall
- [ ] Release workflow + `v0.1.0` tag
- [ ] Launch posts: r/AWSCertifications, r/aws, LinkedIn, dev.to write-up
- **Demo:** a stranger can install from the README without help.

### Out of scope for v1
Deleting resources, Azure/GCP, a web dashboard, AWS Organizations multi-account scanning, Price List API, auto-remediation of GuardDuty findings beyond the kill switch. Candidates for v2 (Azure is the natural first expansion).

---

## 13. Success metrics (track in README badge/section)

- GitHub stars and forks
- Install count (ask users to star or open a "Show your install" discussion; no telemetry, ever)
- Cost flagged: users can opt to share digest totals in a discussion thread
- Issues opened by real users (a good sign)

---

## 14. Working rules for Claude Code

1. **Never touch real AWS without explicit approval.** Do not run `terraform apply`, `terraform destroy`, or any mutating `aws` CLI command unless Devesh says so in that moment. Read-only commands (`aws sts get-caller-identity`, `terraform plan`) are fine.
2. **Use the test account only.** AWS CLI profile `tripwire-test`. Refuse to run against any other profile. Confirm with `aws sts get-caller-identity --profile tripwire-test` before any approved apply.
3. **Never commit secrets.** No tokens, keys, chat IDs, or webhook URLs in code, tests, fixtures, or docs. Use obvious fakes in tests (`AKIAEXAMPLE0000TEST`).
4. **Tests first or alongside.** Every new detector/handler ships with tests in the same change. Run `pytest -q` and `ruff check` before saying a task is done.
5. **Respect the principles in section 3.** If a requested change would delete resources, widen IAM, or add a dependency, stop and flag it.
6. **Small steps.** One milestone checkbox per change where possible. Explain what changed in 2–4 lines.
7. **Verify AWS facts.** Event formats, API names, and prices change. When unsure, check current AWS docs and note the source in a code comment or `docs/`.
8. **Keep docs in sync.** IAM change → update `docs/permissions.md`. New detector → update `docs/detectors.md` and the README table.
9. **Windows-friendly.** Give commands that work in PowerShell or note when Git Bash/WSL is needed.
10. **Update the Status section below** at the end of each session.
11. **File changes use Edit/Write only.** Shell is for running commands (pytest, ruff, git), never for writing files.
12. Never run git push. Devesh pushes manually.

---

## 15. Status

- **Current milestone:** **M1 complete** (2026-10-24). Demo passed 2026-10-23; scheduled 13:00 UTC run verified 2026-10-24; CI green on GitHub (run #3, commit `4a64a35`). Next up is M2.
- **Last session:** 2026-10-24. Removed `kms:Decrypt` from the sweep role (`1db4f53`): applied to `tripwire-test`, waited 2 min, invoked twice a minute apart: both `notify_sent` telegram + email, no AccessDenied. Same apply shipped the `REPLACE_ME` check and trimmed SSM setup commands. Ran `cleanup_seed.py`: `vol-0c28487641e78b434` deleted, `eipalloc-03428f83dad4e240b` released, verified gone. The test stack (sweep, table, SNS, SSM params) is still deployed; it now finds nothing, so it sends the all-clear only on Mondays.
- **Next steps:**
  1. M2: TTL handler (parser already exists in common/tags.py)
- **Done in M1:** scaffold; models + tags; config, regions, pricing; all six detectors; DynamoDB state; notify (Telegram + email); sweep handler; Terraform module (sweep); seed/cleanup scripts; first deploy + demo; scheduled run verified; kms:Decrypt dropped; seed cleaned up; CI green
- **Notes:**
  - **Lessons from the first deploy (2026-10-23):**
    - Telegram failed four ways before working: 404 (bad token), 400 (chat id still `REPLACE_ME`), 401 (token stored with surrounding quotes), 400 "chat not found" (chat id stored with quotes). Each fix was a re-stored SSM value; nothing caches it (`notify.py` reads SSM on every send).
    - Root cause of the quoted values: pasting into `Read-Host` kept the quotes. The `ssm_setup_commands` output now uses `(Read-Host ...).Trim().Trim('"')` and `--value=$t` (`e6294ba`).
    - Logging only the HTTP status made this slow to debug. notify now logs Telegram's `description` (token and bot id scrubbed) and, once applied, "Telegram not configured" for placeholders.
    - `aws login` credentials need `botocore[crt]` for boto3 scripts (in `requirements-dev.txt`). Terraform and the AWS CLI read them natively.
    - Sync `aws lambda invoke` of the sweep: use `--cli-read-timeout 310` and `AWS_MAX_ATTEMPTS=1`, or a run over 60 s gets retried (double invoke). CloudWatch logs lag ~30 s behind the invoke response.
  - Test deployment: `deploy/test/main.tf` (gitignored): provider `allowed_account_ids = ["****4522****"]`, profile `tripwire-test`, ca-central-1, observe, channels telegram + email, `cli_profile = "tripwire-test"`. Seed resources removed 2026-10-24.
  - Applying a reviewed plan: `terraform -chdir=deploy/test plan "-out=reviewed.tfplan"` (quote the `-out=` argument in PowerShell 5.1 or it fails with "Too many command line arguments"), then `apply reviewed.tfplan`.
  - `aws login` sessions expire; Terraform then fails with "login session has expired". Run `aws login --profile tripwire-test` again.
  - `aws lambda invoke ... --log-type Tail` returns the last 4 KB of logs (base64 `LogResult`), enough to see `notify_sent` without waiting for CloudWatch.
  - Test suite now 173 tests at 10–12.5 s on this laptop (over the 10 s budget). Biggest single cost is moto's first EC2 call (~2 s). Revisit if CI shows the same.
  - Windows Application Control blocks `.venv\Scripts\pytest.exe`; run `.\.venv\Scripts\python.exe -m pytest` instead.
  - `pre-commit` added to `requirements-dev.txt` (needed for hooks).
  - Ruff excludes `*.md` so spec code samples aren't reformatted.
  - gitleaks pre-commit hook is a local hook using gitleaks on PATH (`winget install Gitleaks.Gitleaks`); Smart App Control blocks the binary pre-commit builds.
  - State table layout is in `docs/architecture.md`. Terraform must set the table's TTL attribute to `expires_at`. `resolve_missing` takes the (region, detector) pairs that succeeded; a failed pair never resolves its findings, other detectors in that region still do.
  - Sweep handler: regions run sequentially; `duration_s` is in the logged `sweep_summary`. Messages: findings → digest; failures → always sent with "Warning: N checks failed" (⚠️ header if nothing found); otherwise all-clear only on Mondays UTC. Each finding is logged with its `fix_command` (the digest footer points there).
  - Pre-flight failures (config, `describe_regions`, DynamoDB) make the Lambda error. Covered by the `cloudtripwire-sweep-errors` alarm (Errors ≥ 1/day → SNS). **That alarm only reaches email**, not Telegram: a Telegram-only user won't hear about a crashed sweep. Terraform now warns (a `check` block, never fails) when `alert_email` is empty. A Telegram forwarder for the alarm is a possible later fix.
  - Terraform: AWS provider `~> 6.0` (6.70.0 latest on 2026-10-23), archive `~> 2.8`. `data.aws_region.current.region` (v6 name). Module zips `../src` with `archive_file` (so it must be sourced from the full repo, e.g. `github.com/dev-95/cloudtripwire//terraform`); `**` excludes verified against archive v2.8.1 docs, but the zip itself has not been built (needs plan).
  - Terraform creates the Telegram SSM params with `REPLACE_ME` + `ignore_changes = [value]`; the user sets real values with the `ssm_setup_commands` output. Only created when `telegram` is in `channels`.
  - No PITR on the state table (costs money; data is rebuildable). `.terraform.lock.hcl` and `.build/` are gitignored (module consumers own their lock file).
  - `kms:Decrypt` removed 2026-10-24: the `aws/ssm` key policy covers SSM decryption (verified in the test account). A customer managed key would need it back (noted in `docs/permissions.md`).
  - IAM section 8 updated: sweep also needs `elasticloadbalancing:DescribeTags` (elb_no_targets) and only `dynamodb:UpdateItem` + `Scan`.
  - `format_digest(..., checks_failed)` puts the failure warning in the footer, so truncation never drops it.
  - Handler tests monkeypatch `regions.get_client`, `handler._region_clients`, `handler._dynamodb`, `handler._now`, and `notify.send`. `conftest.py` keeps its own reference to `get_client` for `cache_clear()`.
  - A finding resolved and later flagged again gets a new `first_seen` (new episode), not the original one.
  - `notify.py` pins the `botocore` logger to INFO: at DEBUG botocore logs response bodies, including decrypted SSM tokens (the caplog test caught this). Failures log the error type and status/code only, never `str(e)`.
  - Digest footer: "Fix commands: in the sweep Lambda's CloudWatch logs." (spec 6.1 updated). Config rejects `slack` until v0.2; `notify.send` also returns False for it as a backstop.
  - Notify uses `ssm:GetParameter` (one call per secret, matching section 8 IAM) and `sns:Publish`.
  - Prices in `pricing.py` verified 2026-10-19 against the AWS public Price List files (us-east-1); the pricing pages only show examples. Re-verify before release.
  - `Config` includes `sweep_schedule`, `ttl_schedule`, `tags` for completeness; only Terraform actually uses them.
  - `Config.state_table` / `alert_topic_arn` come from `STATE_TABLE` / `ALERT_TOPIC_ARN`, set by Terraform (not user variables, no validation).
  - Tests use the shared `aws` fixture in `tests/conftest.py` (fake creds + `mock_aws`, yields the mock; default AMIs off for speed; use any fake AMI id). Suite ~8s (145 tests). Use `tests/helpers.py` `client()` / `clients()` / `dynamodb()`: they cache boto3 clients per process because building one costs ~0.25–0.4s. `test_helpers.py` proves each mock still starts empty. Nested `mock_aws()` shares state with the outer mock; don't rely on nesting for isolation. pytest-xdist was tried and removed (per-worker warm-up made it slower).
  - moto writes `StateTransitionReason` with "UTC"; real AWS writes "GMT". `ec2_stopped_long.parse_stop_time` accepts both. The unparseable-reason test uses botocore `Stubber` since moto can't produce one.
  - `snapshots_old` skips AWS Backup snapshots by `aws:backup:` tag-key prefix; moto accepts `aws:` tags in tests.
  - `describe_addresses` has no paginator (single call returns all).
  - Fix commands chained with `&&` don't run in Windows PowerShell 5.1; noted in `docs/detectors.md`.
  - `get_client` cache lives for the warm Lambda container (not one invocation); tests call `get_client.cache_clear()`.
  - CI (`ci.yml`) first went green on GitHub at run #3 (commit `4a64a35`). Pins (checked 2026-10-23): checkout v7.0.1, setup-python v7.0.0 (skipped v7.1.0, released the day before), setup-terraform v4.0.1, gitleaks 8.30.1 binary with SHA-256 verified in the job. Terraform job tests 1.5.x (the floor in `versions.tf`) and latest 1.x.
  - `scripts/check_iam.py` is stricter than section 10's grep: it rejects any `*` in an action (e.g. `ec2:Describe*` too), Delete/Terminate/Release (case-insensitive), and fails if a file has no actions (parser miss). Reads HCL `actions`/`not_actions` and JSON/jsonencode `Action`/`NotAction`. Add `cloudformation/cloudtripwire.yaml` to its args when that exists.
  - Local gitleaks on full history (`gitleaks git --redact .`): no leaks, 15 commits.
  - Suite timing is noisy on this laptop: 8.5–13s for 159 tests across runs (same code). The 14 IAM tests take 0.06s. If CI shows it consistently over 10s, the first `ec2` call (~2s warm-up in `test_ebs_finds_unattached_volume`) is the biggest single cost.
- **Open decisions:**
  - Kill switch default mode: `enforce` vs `observe` (section 6.3)
  - CloudFormation one-click at launch vs Terraform-only first (section 10)
- **Blockers:** none
