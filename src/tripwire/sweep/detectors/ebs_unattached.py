"""Detector: unattached EBS volumes."""

from datetime import datetime

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "ebs_unattached"


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    findings = []
    pages = clients.ec2.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )
    for page in pages:
        for vol in page["Volumes"]:
            if is_ignored(tags_to_dict(vol.get("Tags"))):
                continue
            vid, size, vtype = vol["VolumeId"], vol["Size"], vol["VolumeType"]
            findings.append(
                Finding(
                    detector=NAME,
                    resource_id=vid,
                    region=region,
                    monthly_cost_usd=pricing.ebs_monthly(size, vtype),
                    age_days=(now - vol["CreateTime"]).days,
                    reason=f"{size} GB {vtype} volume, not attached to any instance",
                    # Safe to chain: DeleteVolume waits for a pending snapshot to finish.
                    # https://repost.aws/knowledge-center/ebs-resolve-volume-stuck-deleting-state
                    fix_command=(
                        f'aws ec2 create-snapshot --volume-id {vid} --description "Backup of {vid}"'
                        f" --region {region}"
                        f" && aws ec2 delete-volume --volume-id {vid} --region {region}"
                    ),
                )
            )
    return findings
