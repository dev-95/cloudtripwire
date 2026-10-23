"""Detector: instances stopped too long."""

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "ec2_stopped_long"

logger = logging.getLogger(__name__)

# The API has no stop timestamp; AWS writes it into StateTransitionReason, e.g.
# "User initiated (2026-10-01 12:00:00 GMT)". moto writes "UTC" instead of "GMT".
_STOP_TIME = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)\)")


def parse_stop_time(reason: str | None) -> datetime | None:
    if m := _STOP_TIME.search(reason or ""):
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return None


def _volumes_monthly(ec2, volume_ids: list[str]) -> tuple[float, int]:
    """Return (monthly cost, total GB) for the given volumes in one API call."""
    if not volume_ids:
        return 0.0, 0
    volumes = ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]
    cost = sum(pricing.ebs_monthly(v["Size"], v["VolumeType"]) for v in volumes)
    return round(cost, 2), sum(v["Size"] for v in volumes)


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    findings = []
    max_stopped = timedelta(days=config.stopped_instance_days)
    pages = clients.ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                iid = inst["InstanceId"]
                if is_ignored(tags_to_dict(inst.get("Tags"))):
                    continue
                stopped_at = parse_stop_time(inst.get("StateTransitionReason"))
                if stopped_at is None:
                    # Conservative: without a stop time we can't tell how long it has been stopped.
                    logger.warning(
                        json.dumps(
                            {
                                "event": "unparseable_stop_reason",
                                "detector": NAME,
                                "resource": iid,
                                "reason": inst.get("StateTransitionReason"),
                            }
                        )
                    )
                    continue
                stopped_for = now - stopped_at
                if stopped_for <= max_stopped:
                    continue
                vol_ids = [
                    m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if "Ebs" in m
                ]
                cost, total_gb = _volumes_monthly(clients.ec2, vol_ids)
                findings.append(
                    Finding(
                        detector=NAME,
                        resource_id=iid,
                        region=region,
                        monthly_cost_usd=cost,
                        age_days=stopped_for.days,
                        reason=(
                            f"stopped for {stopped_for.days} days, still paying for"
                            f" {len(vol_ids)} EBS volume(s), {total_gb} GB"
                        ),
                        # Wait for the AMI before terminating so the backup is complete.
                        fix_command=(
                            f'aws ec2 create-image --instance-id {iid} --name "backup-{iid}"'
                            f" --region {region}"
                            f" && aws ec2 wait image-available --owners self"
                            f" --filters Name=name,Values=backup-{iid} --region {region}"
                            f" && aws ec2 terminate-instances --instance-ids {iid}"
                            f" --region {region}"
                        ),
                    )
                )
    return findings
