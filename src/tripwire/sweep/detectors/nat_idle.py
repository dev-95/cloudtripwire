"""Detector: idle NAT gateways."""

from datetime import datetime, timedelta

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "nat_idle"

IDLE_BYTES = 1_000_000  # 1 MB over the whole window: health checks and NTP, not real use


def _bytes_out(cloudwatch, nat_id: str, start: datetime, end: datetime) -> float:
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/NATGateway",
        MetricName="BytesOutToDestination",
        Dimensions=[{"Name": "NatGatewayId", "Value": nat_id}],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Sum"],
    )
    # CloudWatch publishes no datapoint for a period with no traffic, so missing = zero bytes.
    return sum(dp["Sum"] for dp in resp["Datapoints"])


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    findings = []
    window = timedelta(days=config.nat_idle_days)
    pages = clients.ec2.get_paginator("describe_nat_gateways").paginate(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    for page in pages:
        for nat in page["NatGateways"]:
            if is_ignored(tags_to_dict(nat.get("Tags"))):
                continue
            # Younger than the window: not enough data yet, and "no datapoints" would look idle.
            if now - nat["CreateTime"] < window:
                continue
            nat_id = nat["NatGatewayId"]
            if _bytes_out(clients.cloudwatch, nat_id, now - window, now) >= IDLE_BYTES:
                continue
            findings.append(
                Finding(
                    detector=NAME,
                    resource_id=nat_id,
                    region=region,
                    monthly_cost_usd=pricing.nat_gateway_monthly(),
                    age_days=(now - nat["CreateTime"]).days,
                    reason=(
                        f"NAT gateway sent under 1 MB in the last {config.nat_idle_days} days"
                        " (hourly charge only; data processing is extra)"
                    ),
                    fix_command=(
                        f"aws ec2 delete-nat-gateway --nat-gateway-id {nat_id} --region {region}"
                    ),
                )
            )
    return findings
