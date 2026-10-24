"""DynamoDB read/write helpers.

One table, string keys `pk` / `sk`. Item kinds:
    FINDING#<region>#<resource_id> / <detector>      finding history (first_seen, status)
    ACTION#<yyyy-mm-dd>            / <iso ts>#<id>   action log, expires after 90 days
    ALERT#<resource_id>            / <alert_type>    "alert once" markers

`table` is a boto3 Table resource. Timestamps are stored as UTC ISO 8601 strings.
"""

import dataclasses
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from tripwire.common.models import Action, Finding

OPEN, RESOLVED = "open", "resolved"
ACTION_RETENTION = timedelta(days=90)
TTL_ATTRIBUTE = "expires_at"  # epoch seconds; must match the table's TTL setting in Terraform


def _finding_key(region: str, resource_id: str, detector: str) -> dict[str, str]:
    return {"pk": f"FINDING#{region}#{resource_id}", "sk": detector}


def _is_conditional_failure(e: ClientError) -> bool:
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _from_dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()}


def _upsert_finding(table, f: Finding, now: datetime) -> str:
    """Write one finding and return its first_seen (ISO)."""
    common = {
        "Key": _finding_key(f.region, f.resource_id, f.detector),
        "ExpressionAttributeNames": {"#s": "status", "#r": "region"},
        "ExpressionAttributeValues": {
            ":now": now.isoformat(),
            ":open": OPEN,
            ":cost": Decimal(str(f.monthly_cost_usd)),  # boto3 rejects floats
            ":region": f.region,
            ":rid": f.resource_id,
        },
        "ReturnValues": "ALL_NEW",
    }
    fields = (
        "last_seen = :now, #s = :open, monthly_cost_usd = :cost, #r = :region, resource_id = :rid"
    )
    try:
        # New or still open: keep the original first_seen.
        resp = table.update_item(
            UpdateExpression=f"SET first_seen = if_not_exists(first_seen, :now), {fields}",
            ConditionExpression="attribute_not_exists(pk) OR #s = :open",
            **common,
        )
    except ClientError as e:
        if not _is_conditional_failure(e):
            raise
        # Was resolved and is back (e.g. a volume detached again): a new episode starts now.
        resp = table.update_item(
            UpdateExpression=f"SET first_seen = :now, {fields} REMOVE resolved_at", **common
        )
    return resp["Attributes"]["first_seen"]


def record_findings(table, findings: Iterable[Finding], now: datetime) -> list[Finding]:
    """Upsert findings; return them with age_days counted from first_seen."""
    out = []
    for f in findings:
        first_seen = datetime.fromisoformat(_upsert_finding(table, f, now))
        out.append(dataclasses.replace(f, age_days=(now - first_seen).days))
    return out


def open_findings(table) -> list[dict[str, Any]]:
    """All open finding items, numbers converted back to float.

    A filtered Scan reads the whole table, which is fine at the expected scale: one
    account has tens of findings and a few action/alert items a day (well under 1 MB,
    a single page, inside the DynamoDB free tier). Add a GSI on status if that changes.
    """
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("pk").begins_with("FINDING#") & Attr("status").eq(OPEN)
    }
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(_from_dynamo(i) for i in resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def resolve_missing(
    table, current: Iterable[Finding], scanned: Iterable[tuple[str, str]], now: datetime
) -> list[dict[str, Any]]:
    """Mark open findings resolved when this sweep no longer flags them.

    `scanned` holds the (region, detector) pairs that ran successfully. Only findings
    from those pairs are touched: a check that errored must never resolve its findings,
    or they'd reopen next run with a reset first_seen. Other detectors in the same
    region still resolve normally. Returns the items that were resolved.
    """
    flagged = {(f.region, f.resource_id, f.detector) for f in current}
    ok_pairs = set(scanned)
    resolved = []
    for item in open_findings(table):
        key = (item["region"], item["resource_id"], item["sk"])
        if (item["region"], item["sk"]) not in ok_pairs or key in flagged:
            continue
        try:
            table.update_item(
                Key={"pk": item["pk"], "sk": item["sk"]},
                UpdateExpression="SET #s = :resolved, resolved_at = :now",
                ConditionExpression="#s = :open",  # skip if something else changed it
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":resolved": RESOLVED,
                    ":open": OPEN,
                    ":now": now.isoformat(),
                },
            )
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise
            continue
        resolved.append(item | {"status": RESOLVED, "resolved_at": now.isoformat()})
    return resolved


def log_action(table, action: Action) -> None:
    ts = action.timestamp
    table.put_item(
        Item=action.to_dict()
        | {
            "pk": f"ACTION#{ts.date().isoformat()}",
            "sk": f"{ts.isoformat()}#{action.resource_id}",
            TTL_ATTRIBUTE: int((ts + ACTION_RETENTION).timestamp()),
        }
    )


def should_alert(
    table, resource_id: str, alert_type: str, now: datetime, keep_for: timedelta | None = None
) -> bool:
    """True the first time it's called for (resource_id, alert_type), False afterwards.

    Check and record are one conditional write, so two concurrent runs can't both alert.
    keep_for sets the marker's DynamoDB TTL, for markers that are only useful briefly.
    """
    item = {"pk": f"ALERT#{resource_id}", "sk": alert_type, "alerted_at": now.isoformat()}
    if keep_for is not None:
        item[TTL_ATTRIBUTE] = int((now + keep_for).timestamp())
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise
    return True
