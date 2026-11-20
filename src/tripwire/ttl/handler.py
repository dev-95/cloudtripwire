"""TTL Lambda entry point.

Hourly: in every enabled region, read the `ttl` tag on running EC2 instances and
available RDS DB instances. Warn an hour before expiry; at expiry, stop the resource
(enforce) or say what would be stopped (observe). Every alert type is sent once per
resource and expiry, so an hourly run doesn't repeat itself. Rules: docs/ttl.md.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError

from tripwire.common import notify, regions, state
from tripwire.common.config import Config, load_config
from tripwire.common.models import Action
from tripwire.common.tags import (
    ASG_TAG,  # noqa: F401  (tests use handler.ASG_TAG)
    ec2_blocked_reason,
    get_ttl_value,
    is_expired,
    is_ignored,
    needs_warning,
    parse_ttl,
    tags_to_dict,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STOPPED_AT_TAG = "tripwire:stopped-at"
MAX_TAG_VALUE_SHOWN = 40  # the ttl value is user input; keep it short in alerts
FAILURE_MARKER_TTL = timedelta(days=2)  # daily failure markers clean themselves up


@cache
def _dynamodb():
    return boto3.resource("dynamodb")


def _now() -> datetime:
    return datetime.now(UTC)


def _region_clients(region: str) -> tuple[Any, Any]:
    return regions.get_client("ec2", region), regions.get_client("rds", region)


@dataclass(frozen=True)
class Resource:
    kind: Literal["ec2", "rds"]
    resource_id: str  # instance id, or DB instance identifier
    region: str
    created_at: datetime  # EC2 LaunchTime, RDS InstanceCreateTime
    tags: dict[str, str]
    arn: str = ""  # RDS only: tagging needs the ARN
    blocked: str = ""  # why it must never be stopped (alert only); "" = stoppable

    @property
    def label(self) -> str:
        noun = "EC2 instance" if self.kind == "ec2" else "RDS DB instance"
        return f"{noun} {self.resource_id} in {self.region}"


@dataclass(frozen=True)
class Verdict:
    status: Literal["ok", "unreadable", "warning", "expired"]
    rule: str = ""  # "ttl 4h", or the lab mode rule
    expiry: datetime | None = None


def rds_blocked_reason(db: dict[str, Any]) -> str:
    # Source: RDS User Guide, "Stopping an Amazon RDS DB instance temporarily" (2026-10-24).
    if cluster := db.get("DBClusterIdentifier"):
        return f"it is a member of Aurora cluster {cluster}; stop the cluster instead"
    if db.get("ReadReplicaSourceDBInstanceIdentifier") or db.get(
        "ReadReplicaSourceDBClusterIdentifier"
    ):
        return "it is a read replica, which RDS can't stop"
    if db.get("ReadReplicaDBInstanceIdentifiers"):
        return "it has read replicas, and RDS can't stop a source instance"
    if db.get("Engine", "").startswith("sqlserver") and db.get("MultiAZ"):
        return "RDS can't stop a Multi-AZ SQL Server instance"
    return ""


def list_ec2(ec2, region: str) -> list[Resource]:
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    out = []
    for page in pages:
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = tags_to_dict(inst.get("Tags"))
                out.append(
                    Resource(
                        kind="ec2",
                        resource_id=inst["InstanceId"],
                        region=region,
                        created_at=inst["LaunchTime"],
                        tags=tags,
                        blocked=ec2_blocked_reason(inst, tags),
                    )
                )
    return out


def list_rds(rds, region: str) -> list[Resource]:
    # DescribeDBInstances returns TagList, so no rds:ListTagsForResource call is needed.
    out = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            if db["DBInstanceStatus"] != "available":
                continue
            out.append(
                Resource(
                    kind="rds",
                    resource_id=db["DBInstanceIdentifier"],
                    region=region,
                    created_at=db["InstanceCreateTime"],
                    tags=tags_to_dict(db.get("TagList")),
                    arn=db["DBInstanceArn"],
                    blocked=rds_blocked_reason(db),
                )
            )
    return out


def evaluate(res: Resource, config: Config, now: datetime) -> Verdict:
    """Pure TTL decision for one resource. Ignored resources are always "ok"."""
    if is_ignored(res.tags):
        return Verdict("ok")
    value = get_ttl_value(res.tags)
    if value is None:
        if not config.lab_mode:
            return Verdict("ok")
        rule = f"lab mode: no ttl tag, {config.lab_grace_hours}h grace"
        expiry = res.created_at.astimezone(UTC) + timedelta(hours=config.lab_grace_hours)
    else:
        rule = f"ttl {value.strip()[:MAX_TAG_VALUE_SHOWN]}"
        expiry = parse_ttl(value, res.created_at)
        if expiry is None:
            return Verdict("unreadable", rule)
    if is_expired(expiry, now):
        return Verdict("expired", rule, expiry)
    if needs_warning(expiry, now):
        return Verdict("warning", rule, expiry)
    return Verdict("ok", rule, expiry)


def humanize(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def undo_command(res: Resource) -> str:
    if res.kind == "ec2":
        return f"aws ec2 start-instances --instance-ids {res.resource_id} --region {res.region}"
    return (
        f"aws rds start-db-instance --db-instance-identifier {res.resource_id}"
        f" --region {res.region}"
    )


def stop(res: Resource, ec2, rds, now: datetime) -> list[str]:
    """Stop and tag one resource. Returns problems that didn't prevent the stop."""
    stamp = [{"Key": STOPPED_AT_TAG, "Value": now.isoformat(timespec="seconds")}]
    if res.kind == "ec2":
        ec2.stop_instances(InstanceIds=[res.resource_id])
    else:
        rds.stop_db_instance(DBInstanceIdentifier=res.resource_id)
    try:  # the stop already happened: a tagging failure must not hide it
        if res.kind == "ec2":
            ec2.create_tags(Resources=[res.resource_id], Tags=stamp)
        else:
            rds.add_tags_to_resource(ResourceName=res.arn, Tags=stamp)
    except Exception as e:
        return [f"tag_failed: {type(e).__name__}"]
    return []


def _alert_key(res: Resource) -> str:
    # RDS identifiers are unique per region only.
    return f"{res.region}#{res.resource_id}"


def handle_resource(
    res: Resource, config: Config, now: datetime, table, ec2, rds
) -> tuple[str, str] | None:
    """Apply the TTL rules to one resource. Returns (outcome, alert line) or None."""
    verdict = evaluate(res, config, now)
    if verdict.status == "ok":
        return None

    key = _alert_key(res)
    if verdict.status == "unreadable":
        value = (get_ttl_value(res.tags) or "").strip()[:MAX_TAG_VALUE_SHOWN]
        if not state.should_alert(table, key, f"ttl_unreadable#{value}", now):
            return None
        return "unreadable", (
            f"• Unreadable ttl tag {value!r} on {res.label}; nothing will be done."
            " Use 30m, 4h, 2d, 1w, 2026-10-20 or 2026-10-20T18:00Z."
        )

    assert verdict.expiry is not None
    expiry_key = verdict.expiry.isoformat()
    if verdict.status == "warning":
        if res.blocked or not state.should_alert(table, key, f"ttl_warning#{expiry_key}", now):
            return None  # blocked resources get one alert, at expiry
        left = humanize(verdict.expiry - now)
        return "warning", f"• Expires in {left}: {res.label} ({verdict.rule})"

    timing = f"{verdict.rule}, expired {humanize(now - verdict.expiry)} ago"
    if res.blocked:
        if not state.should_alert(table, key, f"ttl_expired#{expiry_key}", now):
            return None
        return "not_stoppable", f"• Won't stop {res.label} ({timing}): {res.blocked}"
    if config.mode == "observe":
        if not state.should_alert(table, key, f"ttl_expired#{expiry_key}", now):
            return None
        return "would_stop", f"• Would stop {res.label} ({timing})"

    problems = stop(res, ec2, rds, now)
    undo = undo_command(res)
    action = Action(
        action="stop_instance" if res.kind == "ec2" else "stop_db",
        resource_id=res.resource_id,
        region=res.region,
        mode="enforce",
        performed=True,
        timestamp=now,
        reason=timing,
        undo_command=undo,
    )
    try:  # same as tagging: the stop must still reach the alert
        state.log_action(table, action)
    except Exception as e:
        problems.append(f"log_action_failed: {type(e).__name__}")
    # The CloudWatch log is the fallback record if the DynamoDB write failed.
    logger.log(
        logging.ERROR if problems else logging.INFO,
        json.dumps({"event": "stopped"} | action.to_dict() | {"problems": problems}),
    )
    return "stopped", f"• Stopped {res.label} ({timing}). Undo: {undo}"


def run(
    region_names: list[str], config: Config, now: datetime, table
) -> tuple[dict[str, list[str]], int, list[tuple[str, str]]]:
    """Return (alert lines by outcome, resources checked, failures as (region, error code))."""
    lines: dict[str, list[str]] = {}
    checked = 0
    failures: list[tuple[str, str]] = []
    for region in region_names:
        ec2, rds = _region_clients(region)
        for kind, lister, client in (("ec2", list_ec2, ec2), ("rds", list_rds, rds)):
            try:
                resources = lister(client, region)
            except Exception as e:  # one broken region/service must not stop the run
                failures.append((region, _log_failure(region, kind, e)))
                continue
            for res in resources:
                checked += 1
                try:
                    result = handle_resource(res, config, now, table, ec2, rds)
                except Exception as e:
                    failures.append((region, _log_failure(region, res.resource_id, e)))
                    continue
                if result:
                    outcome, line = result
                    lines.setdefault(outcome, []).append(line)
                    logger.info(
                        json.dumps({"event": "ttl_alert", "outcome": outcome, "line": line})
                    )
    return lines, checked, failures


def _error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") or "ClientError"
    return type(error).__name__


def _log_failure(region: str, target: str, error: Exception) -> str:
    """Log one failed check and return its error code."""
    code = _error_code(error)
    logger.error(
        json.dumps(
            {
                "event": "check_failed",
                "region": region,
                "target": target,
                "error": code,
                # Only EC2/RDS/DynamoDB API errors reach here; no secrets in scope.
                "message": str(error)[:500],
            }
        )
    )
    return code


def failures_to_report(failures: list[tuple[str, str]], table, now: datetime) -> int:
    """Failed checks whose (region, error code) hasn't been reported today (UTC).

    The TTL Lambda runs hourly; without this, a lasting failure (an SCP-denied region,
    a missing permission) would send the same warning 24 times a day.
    """
    day = now.date().isoformat()
    fresh = {
        (region, code)
        for region, code in sorted(set(failures))
        if state.should_alert(
            table, region, f"ttl_check_failed#{code}#{day}", now, keep_for=FAILURE_MARKER_TTL
        )
    }
    return sum(1 for f in failures if f in fresh)


ORDER = ("stopped", "would_stop", "not_stoppable", "warning", "unreadable")


def build_message(lines: dict[str, list[str]], mode: str, checks_failed: int) -> str | None:
    """One message per run when there is something to say or a check failed."""
    count = sum(len(v) for v in lines.values())
    if not count and not checks_failed:
        return None
    checks = "check" if checks_failed == 1 else "checks"
    warning = [f"Warning: {checks_failed} {checks} failed, see logs"] if checks_failed else []
    mode_line = "Mode: observe (nothing was changed)" if mode == "observe" else "Mode: enforce"
    footer = []
    if lines.get("stopped"):
        footer.append(
            "Restarting? Change or remove the ttl tag first, or the next run may stop it again."
        )
    footer += [*warning, mode_line]
    if count:
        header = f"⏰ CloudTripwire TTL: {count} {'update' if count == 1 else 'updates'}"
    else:
        header = "⚠️ CloudTripwire TTL: nothing to report in the checks that ran"
    body = [line for outcome in ORDER for line in lines.get(outcome, [])]
    return notify.fit_message(header, body, footer)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    now = _now()
    config = load_config()
    region_names = regions.enabled_regions(config.regions)
    table = _dynamodb().Table(config.state_table)

    lines, checked, failures = run(region_names, config, now, table)
    reported = failures_to_report(failures, table, now)

    message = build_message(lines, config.mode, reported)
    if message:
        sent = notify.send(
            message,
            message.splitlines()[0],
            config,
            regions.get_client("ssm"),
            regions.get_client("sns"),
        )
        logger.info(json.dumps({"event": "notify_result", "channels": sent}))

    summary: dict[str, Any] = {
        "mode": config.mode,
        "regions_scanned": len(region_names),
        "resources_checked": checked,
        "checks_failed": len(failures),
        "failures_reported": reported,
    }
    summary |= {outcome: len(lines.get(outcome, [])) for outcome in ORDER}
    summary["duration_s"] = round(time.monotonic() - started, 2)
    logger.info(json.dumps({"event": "ttl_summary"} | summary))
    return summary
