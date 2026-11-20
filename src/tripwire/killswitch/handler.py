"""Kill switch Lambda entry point.

Triggered by an AWS Health exposed-credentials event or a GuardDuty finding
(docs/killswitch.md). In order, each step independent of the others (CLAUDE.md 6.3):
1. Deactivate the access key (set Inactive, never deleted).
2. In every enabled region, stop instances launched in the last
   killswitch_lookback_minutes and tag them tripwire:quarantined=true.
3. Send one alert with what was done, what failed, and the manual next steps.
killswitch_mode = observe reports what would happen and changes nothing.

The full key ID is only sent to IAM and written to the DynamoDB action log (so the undo
command works). Logs and alerts show the last 4 characters. IAM error messages contain
the key ID, so only error codes are logged, never the message.
"""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

from tripwire.common import notify, regions, state
from tripwire.common.config import Config, load_config
from tripwire.common.models import Action
from tripwire.common.tags import ec2_blocked_reason, is_ignored, tags_to_dict
from tripwire.killswitch.events import KillTarget, mask_key, parse_event

logger = logging.getLogger()
logger.setLevel(logging.INFO)

QUARANTINE_TAG = "tripwire:quarantined"
ORDER = (
    "failed",
    "deactivated",
    "would_deactivate",
    "not_deactivatable",
    "stopped",
    "would_stop",
    "not_stoppable",
)
NEXT_STEPS = (
    "Next steps:",
    "1. Rotate the key: create a new one, update whatever used it, then delete the old one.",
    "2. Check CloudTrail Event history, in every region, for what the key did.",
    "3. Look for IAM users, roles, access keys and instances you didn't create.",
    "4. Open an AWS Support case (Account and billing) about charges from the leak.",
)
NEXT_STEPS_NO_KEY = (
    "Next steps:",
    "1. Find who launched the instance (CloudTrail RunInstances) and rotate those credentials.",
    *NEXT_STEPS[2:],
)


@cache
def _dynamodb():
    return boto3.resource("dynamodb")


def _now() -> datetime:
    return datetime.now(UTC)


def _error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") or "ClientError"
    return type(error).__name__


class Report:
    """Alert lines by outcome. Every line is also logged."""

    def __init__(self) -> None:
        self.lines: dict[str, list[str]] = {}

    def add(self, outcome: str, line: str, **fields: Any) -> None:
        self.lines.setdefault(outcome, []).append(line)
        level = logging.ERROR if outcome == "failed" else logging.INFO
        logger.log(level, json.dumps({"event": "killswitch_step", "outcome": outcome, **fields}))

    def failed(self, step: str, line: str, error: Exception, **fields: Any) -> None:
        code = _error_code(error)
        self.add("failed", f"{line} ({code})", step=step, error=code, **fields)

    def count(self, outcome: str) -> int:
        return len(self.lines.get(outcome, []))


def key_undo(user: str, key_id: str) -> str:
    return f"aws iam update-access-key --user-name {user} --access-key-id {key_id} --status Active"


def instance_undo(instance_id: str, region: str) -> str:
    return f"aws ec2 start-instances --instance-ids {instance_id} --region {region}"


def _log_action(table, action: Action, report: Report, shown: dict[str, str]) -> None:
    """Write the action log; a failure is reported but never hides the action.

    `shown` overrides fields for the CloudWatch log (the masked key ID).
    """
    logged = action.to_dict() | shown
    try:
        state.log_action(table, action)
    except Exception as e:
        report.failed(
            "log_action", f"• Couldn't write the action log for {logged['resource_id']}", e
        )
    logger.info(json.dumps({"event": "action"} | logged))


def deactivate_key(target: KillTarget, mode: str, now: datetime, table, report: Report) -> None:
    key = target.access_key_id
    if key is None:
        return
    shown = mask_key(key)
    if key.startswith("ASIA") or target.user_type not in (None, "IAMUser"):
        # Temporary STS credentials (e.g. an instance role): UpdateAccessKey can't touch them.
        how = (
            f"revoke the role's sessions (IAM console > Roles > {target.user_name}"
            " > Revoke active sessions)"
            if target.user_type == "AssumedRole" and target.user_name
            else "revoke them from the IAM console"
        )
        report.add(
            "not_deactivatable",
            f"• Can't deactivate key {shown}: it is a temporary credential; {how}.",
            key=shown,
        )
        return

    iam = regions.get_client("iam")
    try:
        if target.user_type == "IAMUser" and target.user_name:
            user = target.user_name
        else:  # Health events don't name the user
            user = iam.get_access_key_last_used(AccessKeyId=key)["UserName"]
    except Exception as e:
        report.failed(
            "find_key_user",
            f"• Couldn't find the IAM user for key {shown}; deactivate it in the IAM console",
            e,
            key=shown,
        )
        return

    if mode == "observe":
        report.add("would_deactivate", f"• Would deactivate key {shown} (user {user})", key=shown)
        return
    try:
        iam.update_access_key(UserName=user, AccessKeyId=key, Status="Inactive")
    except Exception as e:
        report.failed(
            "deactivate_key",
            f"• Couldn't deactivate key {shown} (user {user}); do it in the IAM console now",
            e,
            key=shown,
        )
        return
    undo_shown = key_undo(user, shown)
    report.add(
        "deactivated",
        f"• Deactivated key {shown} (user {user}). Undo (full key ID in the action log):"
        f" {undo_shown}",
        key=shown,
    )
    action = Action(
        action="deactivate_key",
        resource_id=key,
        region="global",
        mode="enforce",
        performed=True,
        timestamp=now,
        reason=f"{target.source}: {target.finding_type}",
        undo_command=key_undo(user, key),
    )
    _log_action(table, action, report, {"resource_id": shown, "undo_command": undo_shown})


def recent_instances(instances: list[dict[str, Any]], cutoff: datetime) -> list[tuple[str, str]]:
    """(instance id, blocked reason) for instances launched at or after cutoff.

    Pure. Ignored instances are left out entirely (never acted on, never flagged).
    """
    out = []
    for inst in instances:
        tags = tags_to_dict(inst.get("Tags"))
        if inst["LaunchTime"] < cutoff or is_ignored(tags):
            continue
        out.append((inst["InstanceId"], ec2_blocked_reason(inst, tags)))
    return out


def _describe(ec2) -> list[dict[str, Any]]:
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}]
    )
    return [i for page in pages for r in page["Reservations"] for i in r["Instances"]]


def stop_recent(target: KillTarget, config: Config, now: datetime, table, report: Report) -> int:
    """Stop recently launched instances in every enabled region. Returns regions checked."""
    try:
        region_names = regions.enabled_regions(config.regions)
    except Exception as e:
        report.failed("list_regions", "• Couldn't list regions, so no instances were checked", e)
        return 0
    cutoff = now - timedelta(minutes=config.killswitch_lookback_minutes)
    for region in region_names:
        ec2 = regions.get_client("ec2", region)
        try:
            found = recent_instances(_describe(ec2), cutoff)
        except Exception as e:  # one broken region must not stop the others
            report.failed("describe_instances", f"• Couldn't check {region}", e, region=region)
            continue
        for instance_id, blocked in found:
            label = f"instance {instance_id} in {region}"
            if blocked:
                report.add("not_stoppable", f"• Won't stop {label}: {blocked}", resource=label)
            elif config.killswitch_mode == "observe":
                report.add("would_stop", f"• Would stop {label}", resource=label)
            else:
                _stop(instance_id, region, ec2, target, now, table, report)
    return len(region_names)


def _stop(instance_id, region, ec2, target: KillTarget, now, table, report: Report) -> None:
    label = f"instance {instance_id} in {region}"
    try:
        ec2.stop_instances(InstanceIds=[instance_id])
    except Exception as e:
        report.failed("stop_instance", f"• Couldn't stop {label}", e, resource=label)
        return
    undo = instance_undo(instance_id, region)
    report.add("stopped", f"• Stopped {label}. Undo: {undo}", resource=label)
    try:  # the stop already happened: a tagging failure must not hide it
        ec2.create_tags(Resources=[instance_id], Tags=[{"Key": QUARANTINE_TAG, "Value": "true"}])
    except Exception as e:
        report.failed("tag_instance", f"• Stopped {label} but couldn't tag it", e, resource=label)
    action = Action(
        action="stop_instance",
        resource_id=instance_id,
        region=region,
        mode="enforce",
        performed=True,
        timestamp=now,
        reason=f"launched in the last kill switch window; {target.source}: {target.finding_type}",
        undo_command=undo,
    )
    _log_action(table, action, report, {})


def build_message(target: KillTarget, report: Report, mode: str) -> str:
    key = mask_key(target.access_key_id) if target.access_key_id else None
    header = f"🚨 CloudTripwire kill switch: {'leaked key ' + key if key else 'GuardDuty alert'}"
    trigger = (
        "AWS Health: key exposed publicly (AWS_RISK_CREDENTIALS_EXPOSED)"
        if target.source == "health"
        else f"GuardDuty {target.finding_type}"
    )
    if target.instance_id:
        trigger += f", instance {target.instance_id}"
    intro = [f"Trigger: {trigger}", f"Account: {target.account_id or 'unknown'}"]
    body = [line for outcome in ORDER for line in report.lines.get(outcome, [])]
    if not body:
        body = ["• Nothing to stop: no instances launched in the lookback window."]
    mode_line = (
        'Mode: observe (nothing was changed; set killswitch_mode = "enforce" to act)'
        if mode == "observe"
        else "Mode: enforce"
    )
    footer = [*(NEXT_STEPS if key else NEXT_STEPS_NO_KEY), mode_line]
    return notify.fit_message(header, intro + body, footer)


def sample_message(target: KillTarget) -> str:
    return notify.fit_message(
        "🧪 CloudTripwire kill switch: sample finding received, no action taken",
        [f"Trigger: GuardDuty {target.finding_type} (sample)"],
        ["GuardDuty sample findings never deactivate keys or stop instances, in any mode."],
    )


def _send(message: str, config: Config) -> None:
    try:
        sent = notify.send(
            message,
            message.splitlines()[0],
            config,
            regions.get_client("ssm"),
            regions.get_client("sns"),
        )
    except Exception as e:  # notify handles channel errors; this is a last resort
        sent = {}
        logger.error(json.dumps({"event": "notify_failed", "error": _error_code(e)}))
    logger.info(json.dumps({"event": "notify_result", "channels": sent}))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    target = parse_event(event)  # logs and returns None for anything it can't use
    if target is None:
        return {"status": "skipped"}
    config = load_config()
    mode = config.killswitch_mode

    if target.sample:  # checked before any AWS call: zero mutating calls in any mode
        logger.info(json.dumps({"event": "sample_finding", "finding_type": target.finding_type}))
        _send(sample_message(target), config)
        return {"status": "sample", "mode": mode, "finding_type": target.finding_type}

    now = _now()
    table = _dynamodb().Table(config.state_table)
    report = Report()

    deactivate_key(target, mode, now, table, report)
    regions_checked = stop_recent(target, config, now, table, report)
    _send(build_message(target, report, mode), config)

    summary: dict[str, Any] = {
        "status": "handled",
        "mode": mode,
        "source": target.source,
        "finding_type": target.finding_type,
        "regions_checked": regions_checked,
    }
    summary |= {outcome: report.count(outcome) for outcome in ORDER}
    summary["duration_s"] = round(time.monotonic() - started, 2)
    logger.info(json.dumps({"event": "killswitch_summary"} | summary))
    return summary
