"""Fallback price table + monthly cost helpers.

All prices are us-east-1 on-demand list prices (USD) and are estimates for every
other region. Re-verified 2026-11-20 (first checked 2026-10-19): every value below
matched the AWS Price List. IPv4 and load balancers: the public offer files below
(published 2026-11-19). EBS, snapshots, NAT: the Price List Query API
(`pricing get-products`, ServiceCode AmazonEC2, location "US East (N. Virginia)"),
because their metered-unit files were last modified in 2024. The human-readable
pricing pages only show worked examples with the same numbers.
"""

import json
import logging
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730

# EBS storage per GB-month (storage only; provisioned IOPS/throughput not included).
# Source: Price List Query API, productFamily "Storage", volumeApiName <type>
#   (usagetype EBS:VolumeUsage.<type>; io1 is EBS:VolumeUsage.piops, standard is
#   EBS:VolumeUsage). Also in meteredUnitMaps/ec2/USD/current/ebs.json.
# Page: https://aws.amazon.com/ebs/pricing/ (verified 2026-11-20)
EBS_GB_MONTH = {
    "gp3": 0.08,
    "gp2": 0.10,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,  # magnetic, previous generation
}

# EBS snapshot, standard tier, per GB-month. Price List Query API, usagetype
# EBS:SnapshotUsage (verified 2026-11-20).
SNAPSHOT_GB_MONTH = 0.05

# Public IPv4 address, in-use or idle, per hour.
# Source: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonVPC/current/us-east-1/index.json
# usagetype USE1-PublicIPv4:InUseAddress and :IdleAddress, both $0.005.
# Page: https://aws.amazon.com/vpc/pricing/ (verified 2026-11-20)
PUBLIC_IPV4_HOUR = 0.005

# NAT gateway per hour (data processing $0.045/GB not included).
# Source: Price List Query API, productFamily "NAT Gateway", usagetype NatGateway-Hours.
#   Also in meteredUnitMaps/ec2/USD/current/natgateway.json.
# Page: https://aws.amazon.com/vpc/pricing/ (verified 2026-11-20)
NAT_GATEWAY_HOUR = 0.045

# Load balancer per hour (LCU / data charges not included). Gateway LB is per AZ.
# Source: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSELB/current/us-east-1/index.json
# usagetype LoadBalancerUsage per productFamily (Load Balancer-Application/-Network/
#   -Gateway; plain "Load Balancer" is Classic).
# Page: https://aws.amazon.com/elasticloadbalancing/pricing/ (verified 2026-11-20)
LOAD_BALANCER_HOUR = {
    "application": 0.0225,
    "network": 0.0225,
    "gateway": 0.0125,
    "classic": 0.025,
}


def _usd(quantity: float, unit_price: float) -> float:
    # Multiply in Decimal and round half-up: float math sends 1 GB x 0.015 to 0.01.
    amount = Decimal(str(quantity)) * Decimal(str(unit_price))
    return float(amount.quantize(Decimal("0.01"), ROUND_HALF_UP))


def ebs_monthly(size_gb: float, volume_type: str) -> float:
    price = EBS_GB_MONTH.get(volume_type)
    if price is None:
        logger.warning(json.dumps({"event": "unknown_volume_type", "volume_type": volume_type}))
        price = EBS_GB_MONTH["gp2"]
    return _usd(size_gb, price)


def snapshot_monthly(size_gb: float) -> float:
    return _usd(size_gb, SNAPSHOT_GB_MONTH)


def public_ipv4_monthly() -> float:
    return _usd(PUBLIC_IPV4_HOUR, HOURS_PER_MONTH)


def nat_gateway_monthly() -> float:
    return _usd(NAT_GATEWAY_HOUR, HOURS_PER_MONTH)


def load_balancer_monthly(lb_type: str) -> float:
    """lb_type is the ELBv2 `Type` field (application/network/gateway) or 'classic'."""
    return _usd(LOAD_BALANCER_HOUR[lb_type], HOURS_PER_MONTH)
