"""Detector: unassociated Elastic IPs."""

from datetime import datetime

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "eip_unassociated"


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    findings = []
    # describe_addresses has no paginator; it returns every address in one call.
    for addr in clients.ec2.describe_addresses()["Addresses"]:
        if "AssociationId" in addr or is_ignored(tags_to_dict(addr.get("Tags"))):
            continue
        findings.append(
            Finding(
                detector=NAME,
                resource_id=addr["AllocationId"],
                region=region,
                monthly_cost_usd=pricing.public_ipv4_monthly(),
                age_days=None,  # EIPs have no creation time
                reason=f"Elastic IP {addr['PublicIp']} is not attached to anything",
                fix_command=(
                    f"aws ec2 release-address --allocation-id {addr['AllocationId']}"
                    f" --region {region}"
                ),
            )
        )
    return findings
