"""Detector: old unreferenced snapshots."""

from datetime import datetime, timedelta

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "snapshots_old"

# AWS Backup tags the snapshots it creates with aws:backup:* keys (aws:backup:source-resource).
# Deleting one outside AWS Backup breaks its recovery point; the backup plan owns retention.
BACKUP_TAG_PREFIX = "aws:backup:"


def _ami_snapshot_ids(ec2) -> set[str]:
    ids = set()
    for page in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
        for image in page["Images"]:
            for mapping in image.get("BlockDeviceMappings", []):
                if sid := mapping.get("Ebs", {}).get("SnapshotId"):
                    ids.add(sid)
    return ids


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    findings = []
    max_age = timedelta(days=config.snapshot_max_age_days)
    in_use = None  # fetched lazily: only needed if some snapshot is old enough
    pages = clients.ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"])
    for page in pages:
        for snap in page["Snapshots"]:
            age = now - snap["StartTime"]
            if age <= max_age:
                continue
            tags = tags_to_dict(snap.get("Tags"))
            if is_ignored(tags) or any(k.lower().startswith(BACKUP_TAG_PREFIX) for k in tags):
                continue
            if in_use is None:
                in_use = _ami_snapshot_ids(clients.ec2)
            sid, size = snap["SnapshotId"], snap["VolumeSize"]
            if sid in in_use:
                continue
            findings.append(
                Finding(
                    detector=NAME,
                    resource_id=sid,
                    region=region,
                    monthly_cost_usd=pricing.snapshot_monthly(size),
                    age_days=age.days,
                    reason=(
                        f"{size} GB snapshot, {age.days} days old, not used by any AMI"
                        " (estimate, billed size is often smaller)"
                    ),
                    fix_command=f"aws ec2 delete-snapshot --snapshot-id {sid} --region {region}",
                )
            )
    return findings
