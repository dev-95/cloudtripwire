# architecture

TODO

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
